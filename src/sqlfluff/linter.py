"""Defines the linter class."""

import os
import time
from collections import namedtuple
from difflib import SequenceMatcher
from benchit import BenchIt

from .errors import SQLLexError, SQLParseError, SQLTemplaterError
from .parser import FileSegment, ParseContext
# We should probably move verbosity logger to somewhere else?
from .parser.segments_base import verbosity_logger, frame_msg
from .rules import get_ruleset


from .cli.formatters import (format_linting_path, format_file_violations,
                             format_filename, format_config_vals)


class LintedFile(namedtuple('ProtoFile', ['path', 'violations', 'time_dict', 'tree', 'file_mask'])):
    """A class to store the idea of a linted file."""
    __slots__ = ()

    def check_tuples(self):
        """Make a list of check_tuples.

        This assumes that all the violations found are
        linting violations (and therefore implement `check_tuple()`).
        If they don't then this function raises that error.
        """
        vs = []
        for v in self.violations:
            if hasattr(v, 'check_tuple'):
                vs.append(v.check_tuple())
            else:
                raise v
        return vs

    def num_violations(self, rules=None, types=None, filter_ignore=True):
        """Count the number of violations.

        Optionally now with filters.
        """
        violations = self.violations
        if types:
            try:
                types = tuple(types)
            except TypeError:
                types = (types,)
            violations = [v for v in violations if isinstance(v, types)]
        if rules:
            if isinstance(rules, str):
                rules = (rules,)
            else:
                rules = tuple(rules)
            violations = [v for v in violations if v.rule_code() in rules]
        # Filter ignorable violations
        if filter_ignore:
            violations = [v for v in violations if not v.ignore]
        return len(violations)

    def is_clean(self):
        """Return True if there are no ignorable violations."""
        return not any(not v.ignore for v in self.violations)

    def fix_string(self, verbosity=0):
        """Obtain the changes to a path as a string.

        We use the file_mask to do a safe merge, avoiding any templated
        sections. First we need to detect where there have been changes
        between the fixed and templated versions. The file mask is of
        the format: (raw_file, templated_file, fixed_file).

        We use difflib.SequenceMatcher.get_opcodes
        See: https://docs.python.org/3.7/library/difflib.html#difflib.SequenceMatcher.get_opcodes
        It returns a list of tuples ('equal|replace|delete|insert', ia1, ia2, ib1, ib2).

        """
        # Do we have enough information to actually fix the file?
        if any(elem is None for elem in self.file_mask):
            verbosity_logger(
                "Insufficient information to fix file: {0}".format(self.file_mask),
                verbosity=verbosity)
            return None, False

        verbosity_logger("Persisting file masks: {0}".format(self.file_mask), verbosity=verbosity)
        # Compare Templated with Raw
        diff_templ = SequenceMatcher(autojunk=None, a=self.file_mask[0], b=self.file_mask[1])
        diff_templ_codes = diff_templ.get_opcodes()
        verbosity_logger("Templater diff codes: {0}".format(diff_templ_codes), verbosity=verbosity)

        # Compare Fixed with Templated
        diff_fix = SequenceMatcher(autojunk=None, a=self.file_mask[1], b=self.file_mask[2])
        # diff_fix = SequenceMatcher(autojunk=None, a=self.file_mask[1][0], b=self.file_mask[2][0])
        diff_fix_codes = diff_fix.get_opcodes()
        verbosity_logger("Fixing diff codes: {0}".format(diff_fix_codes), verbosity=verbosity)

        # If diff_templ isn't the same then we should just keep the template. If there *was*
        # a fix in that space, then we should raise an issue
        # If it is the same, then we can apply fixes as expected.
        write_buff = ''
        fixed_block = None
        templ_block = None
        # index in raw, templ and fix
        idx = (0, 0, 0)
        loop_idx = 0
        while True:
            loop_idx += 1
            verbosity_logger(
                "{0:04d}: Write Loop: idx:{1}, buff:{2!r}".format(loop_idx, idx, write_buff),
                verbosity=verbosity)

            if templ_block is None:
                if diff_templ_codes:
                    templ_block = diff_templ_codes.pop(0)
                # We've exhausted the template. Have we exhausted the fixes?
                elif fixed_block is None and not diff_fix_codes:
                    # Yes - excellent. DONE
                    break
                # Deal with the case that we only have inserts left.
                elif all(elem[0] == 'insert' for elem in diff_fix_codes):
                    for fixed_block in diff_fix_codes:
                        write_buff += self.file_mask[2][fixed_block[3]:fixed_block[4]]
                    break
                else:
                    raise NotImplementedError("Fix Block(s) left over! Don't know how to handle this! aeflf8wh")
            if fixed_block is None:
                if diff_fix_codes:
                    fixed_block = diff_fix_codes.pop(0)
                else:
                    raise NotImplementedError("Unexpectedly depleted the fixes. Panic!")
            verbosity_logger(
                "{0:04d}: Blocks: template:{1}, fix:{2}".format(loop_idx, templ_block, fixed_block),
                verbosity=verbosity)

            if templ_block[0] == 'equal':
                if fixed_block[0] == 'equal':
                    # No templating, no fixes, go with middle and advance indexes
                    # Find out how far we can advance (we use the middle version because it's common)
                    if templ_block[4] == fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:fixed_block[2]]
                        # consume both blocks
                        fixed_block = None
                        templ_block = None
                    elif templ_block[4] > fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:fixed_block[2]]
                        # consume fixed block
                        fixed_block = None
                    elif templ_block[4] < fixed_block[2]:
                        buff = self.file_mask[1][idx[1]:templ_block[4]]
                        # consume templ block
                        templ_block = None
                    idx = (idx[0] + len(buff), idx[1] + len(buff), idx[2] + len(buff))
                    write_buff += buff
                    continue
                elif fixed_block[0] == 'replace':
                    # Consider how to apply fixes.
                    # Can we implement the fix while staying in the equal segment?
                    if fixed_block[2] <= templ_block[4]:
                        # Yes! Write from the fixed version.
                        write_buff += self.file_mask[2][idx[2]:fixed_block[4]]
                        idx = (idx[0] + (fixed_block[2] - fixed_block[1]), fixed_block[2], fixed_block[4])
                        # Consume the fixed block because we've written the whole thing.
                        fixed_block = None
                        continue
                    else:
                        raise NotImplementedError("DEF")
                elif fixed_block[0] == 'delete':
                    # We're deleting items, nothing to write but we can consume some
                    # blocks and advance some indexes.
                    idx = (idx[0] + (fixed_block[2] - fixed_block[1]), fixed_block[2], fixed_block[4])
                    fixed_block = None
                elif fixed_block[0] == 'insert':
                    # We're inserting items, Write from the fix block, but only that index moves.
                    write_buff += self.file_mask[2][idx[2]:fixed_block[4]]
                    idx = (idx[0], idx[1], fixed_block[4])
                    fixed_block = None
                else:
                    raise NotImplementedError(
                        ("Unexpected opcode {0} for fix block! Please report this "
                         "issue on github with the query and rules you're trying to "
                         "fix.").format(fixed_block[0]))
            elif templ_block[0] == 'replace':
                # We're in a templated section - we should write the templated version.
                # we should consume the whole replace block and then deal with where
                # we end up.
                buff = self.file_mask[0][idx[0]:templ_block[2]]
                new_templ_idx = templ_block[4]

                # Fast forward through fix blocks until we catch up. We're not implementing
                # any changes in a templated section.
                while True:
                    if fixed_block[2] > new_templ_idx >= fixed_block[1]:
                        # this block contains the end point
                        break
                    else:
                        # We're not at the end point yet, continue to fast forward through.
                        if fixed_block[0] != 'equal':
                            print("WARNING: Skipping edit block: {0}".format(fixed_block))
                        if diff_fix_codes:
                            fixed_block = diff_fix_codes.pop(0)
                        else:
                            raise NotImplementedError("Unexpectedly depleted the fixes. Panic!")
                # Are we exactly on a join?
                if new_templ_idx == fixed_block[1]:
                    # GREAT - this makes things easy because we have an equality point already
                    idx = (templ_block[2], new_templ_idx, fixed_block[3])
                else:
                    if fixed_block[0] == 'equal':
                        # If it's in an equal block, we can use the same offset from the end.
                        idx = (templ_block[2], new_templ_idx, fixed_block[3] + (new_templ_idx - fixed_block[1]))
                    else:
                        # TODO: We're trying to move through an templated section, but end up
                        # in a fixed section. We've lost track of indexes.
                        # We might need to panic if this happens...
                        print("UMMMMMM!")
                        print(new_templ_idx)
                        print(fixed_block)
                        raise NotImplementedError("ABC")
                write_buff += buff
                # consume template block
                templ_block = None
            elif templ_block[0] == 'delete':
                # The comparison, things that the templater has deleted
                # some characters. This is just a quirk of the differ.
                # In reality this means we just write these characters
                # and don't worry about advancing the other indexes.
                buff = self.file_mask[0][idx[0]:templ_block[2]]
                # consume templ block
                templ_block = None
                idx = (idx[0] + len(buff), idx[1], idx[2])
                write_buff += buff
            elif templ_block[0] == 'insert':
                # The templater has inserted something here. We don't need
                # to write anything here (because whatever we're looking at
                # was inserted by the templater), but we do need to keep
                # track of what happened to the rest of the section we're in.
                # If nothing was fixed then it's easy because the indices
                # will be the same. Otherwise... great question...

                # For now let's just deal with the happy case where the fixed
                # block is equal
                if fixed_block[0] == 'equal':
                    # Let's make sure we can consume enough to get through the
                    # templ block and not get to the end of the fix block.
                    if templ_block[4] <= fixed_block[2]:
                        insert_len = templ_block[4] - templ_block[3]
                        idx = (idx[0], idx[1] + insert_len, idx[2] + insert_len)
                        # if things matched up perfectly, consume the fixed block
                        if templ_block[4] == fixed_block[2]:
                            fixed_block = None
                        # always consume templ block in this case
                        templ_block = None
                    else:
                        raise NotImplementedError(
                            ("Unexpected scenario during insert opcode! Please report "
                             "this issue on github with the query and rules you're trying "
                             "to fix."))
                else:
                    raise NotImplementedError(
                        ("Unexpected opcode {0} for fix block! Please report this "
                         "issue on github with the query and rules you're trying to "
                         "fix.").format(fixed_block[0]))
            else:
                raise NotImplementedError(
                    ("Unexpected opcode {0} for template block! Please report this "
                     "issue on github with the query and rules you're trying to "
                     "fix.").format(templ_block[0]))

        # The success metric here is whether anything ACTUALLY changed.
        return write_buff, write_buff != self.file_mask[0]

    def persist_tree(self, verbosity=0):
        """Persist changes to the given path.

        We use the file_mask to do a safe merge, avoiding any templated
        sections. First we need to detect where there have been changes
        between the fixed and templated versions.

        We use difflib.SequenceMatcher.get_opcodes
        See: https://docs.python.org/3.7/library/difflib.html#difflib.SequenceMatcher.get_opcodes
        It returns a list of tuples ('equal|replace', ia1, ia2, ib1, ib2).

        """
        write_buff, success = self.fix_string(verbosity=verbosity)

        if success:
            # Actually write the file.
            with open(self.path, 'w') as f:
                f.write(write_buff)

        # TODO: Make return value of persist_changes() a more interesting result and then format it
        # click.echo(format_linting_fixes(result, verbose=verbose), color=color)
        return success


class LintedPath:
    """A class to store the idea of a collection of linted files at a single start path."""
    def __init__(self, path):
        self.files = []
        self.path = path

    def add(self, file):
        """Add a file to this path."""
        self.files.append(file)

    def check_tuples(self, by_path=False):
        """Compress all the tuples into one list.

        NB: This is a little crude, as you can't tell which
        file the violations are from. Good for testing though.
        For more control set the `by_path` argument to true.
        """
        if by_path:
            return {file.path: file.check_tuples() for file in self.files}
        else:
            tuple_buffer = []
            for file in self.files:
                tuple_buffer += file.check_tuples()
            return tuple_buffer

    def num_violations(self, **kwargs):
        """Count the number of violations in the path."""
        return sum(file.num_violations(**kwargs) for file in self.files)

    def violations(self):
        """Return a dict of violations by file path."""
        return {file.path: file.violations for file in self.files}

    def stats(self):
        """Return a dict containing linting stats about this path."""
        return dict(
            files=len(self.files),
            clean=sum(file.is_clean() for file in self.files),
            unclean=sum(not file.is_clean() for file in self.files),
            violations=sum(file.num_violations() for file in self.files)
        )

    def persist_changes(self, verbosity=0, output_func=None, **kwargs):
        """Persist changes to files in the given path.

        This also logs the output using the output_func if present.
        """
        # Run all the fixes for all the files and return a dict
        buffer = {}
        for file in self.files:
            if self.num_violations(**kwargs) > 0:
                buffer[file.path] = file.persist_tree(verbosity=verbosity)
                result = buffer[file.path]
            else:
                buffer[file.path] = True
                result = 'SKIP'

            if output_func:
                output_func(
                    format_filename(
                        filename=file.path,
                        success=result,
                        verbose=verbosity)
                )
        return buffer


class LintingResult:
    """A class to represent the result of a linting operation.

    Notably this might be a collection of paths, all with multiple
    potential files within them.
    """

    def __init__(self):
        self.paths = []

    @staticmethod
    def sum_dicts(d1, d2):
        """Take the keys of two dictionaries and add them."""
        keys = set(d1.keys()) | set(d2.keys())
        return {key: d1.get(key, 0) + d2.get(key, 0) for key in keys}

    @staticmethod
    def combine_dicts(*d):
        """Take any set of dictionaries and combine them."""
        dict_buffer = {}
        for dct in d:
            dict_buffer.update(dct)
        return dict_buffer

    def add(self, path):
        """Add a new `LintedPath` to this result."""
        self.paths.append(path)

    def check_tuples(self, by_path=False):
        """Fetch all check_tuples from all contained `LintedPath` objects.

        Args:
            by_path (:obj:`bool`, optional): When False, all the check_tuples
                are aggregated into one flat list. When True, we return a `dict`
                of paths, each with it's own list of check_tuples. Defaults to False.

        """
        if by_path:
            buff = {}
            for path in self.paths:
                buff.update(path.check_tuples(by_path=by_path))
            return buff
        else:
            tuple_buffer = []
            for path in self.paths:
                tuple_buffer += path.check_tuples()
            return tuple_buffer

    def num_violations(self, **kwargs):
        """Count the number of violations in thie result."""
        return sum(path.num_violations(**kwargs) for path in self.paths)

    def violations(self):
        """Return a dict of paths and violations."""
        return self.combine_dicts(path.violations() for path in self.paths)

    def stats(self):
        """Return a stats dictionary of this result."""
        all_stats = dict(files=0, clean=0, unclean=0, violations=0)
        for path in self.paths:
            all_stats = self.sum_dicts(path.stats(), all_stats)
        if all_stats['files'] > 0:
            all_stats['avg per file'] = all_stats['violations'] * 1.0 / all_stats['files']
            all_stats['unclean rate'] = all_stats['unclean'] * 1.0 / all_stats['files']
        else:
            all_stats['avg per file'] = 0
            all_stats['unclean rate'] = 0
        all_stats['clean files'] = all_stats['clean']
        all_stats['unclean files'] = all_stats['unclean']
        all_stats['exit code'] = 65 if all_stats['violations'] > 0 else 0
        all_stats['status'] = 'FAIL' if all_stats['violations'] > 0 else 'PASS'
        return all_stats

    def as_records(self):
        """Return the result as a list of dictionaries.

        Each record contains a key specifying the filepath, and a list of violations. This
        method is useful for serialization as all objects will be builtin python types
        (ints, strs).
        """
        return [
            {'filepath': path, 'violations': [v.get_info_dict() for v in violations]}
            for lintedpath in self.paths
            for path, violations in lintedpath.violations().items()
            if violations
        ]

    def persist_changes(self, verbosity=0, output_func=None, **kwargs):
        """Run all the fixes for all the files and return a dict."""
        return self.combine_dicts(
            *[
                path.persist_changes(verbosity=verbosity, output_func=output_func, **kwargs)
                for path in self.paths
            ]
        )


class Linter:
    """The interface class to interact with the linter."""

    def __init__(self, sql_exts=('.sql',), output_func=None,
                 config=None):
        if config is None:
            raise ValueError("No config object provided to linter!")
        self.dialect = config.get('dialect_obj')
        self.templater = config.get('templater_obj')
        self.sql_exts = sql_exts
        # Used for logging as we go
        self.output_func = output_func
        # Store the config object
        self.config = config

    def get_parse_context(self, config=None):
        """Get a new parse context, optionally from a different config."""
        # Try to use a given config
        if config:
            return ParseContext.from_config(config)
        # Default to the instance config
        elif self.config:
            return ParseContext.from_config(self.config)
        else:
            raise ValueError("No config object!")

    def log(self, msg):
        """Log a message, using the common logging framework."""
        if self.output_func:
            # Check we've actually got a meaningful message
            if msg.strip(' \n\t'):
                self.output_func(msg)

    def get_ruleset(self, config=None):
        """Get hold of a set of rules."""
        rs = get_ruleset()
        cfg = config or self.config
        return rs.get_rulelist(config=cfg)

    def rule_tuples(self):
        """A simple pass through to access the rule tuples of the rule set."""
        rs = self.get_ruleset()
        return [(rule.code, rule.description) for rule in rs]

    def parse_string(self, s, fname=None, verbosity=0, recurse=True, config=None):
        """Parse a string.

        Returns:
            `tuple` of (`parsed`, `violations`, `time_dict`, `config_diff`).
                `parsed` is a segment structure representing the parsed file. If
                    parsing fails due to an inrecoverable violation then we will
                    return None.
                `violations` is a :obj:`list` of violations so far, which will either be
                    templating, lexing or parsing violations at this stage.
                `time_dict` is a :obj:`dict` containing timings for how long each step
                    took in the process.

        """
        violations = []
        t0 = time.monotonic()
        bencher = BenchIt()  # starts the timer
        if fname:
            short_fname = fname.replace('\\', '/').split('/')[-1]
        else:
            # this handles to potential case of a null fname
            short_fname = fname
        bencher("Staring parse_string for {0!r}".format(short_fname))

        # Log the start of this process if we're in a more verbose mode.
        if verbosity > 1:
            self.log(format_filename(filename=fname, success='PARSING', verbose=verbosity))
            # This is where we output config diffs if they exist.
            if config:
                # Only output config diffs if there is a config to diff to.
                config_diff = config.diff_to(self.config)
                if config_diff:
                    self.log("   Config Diff:")
                    self.log(format_config_vals(self.config.iter_vals(cfg=config_diff)))

        verbosity_logger("TEMPLATING RAW [{0}] ({1})".format(self.templater.name, fname), verbosity=verbosity)
        try:
            s = self.templater.process(s, fname=fname, config=config or self.config)
        except SQLTemplaterError as err:
            violations.append(err)
            file_segment = None
            # NB: We'll carry on if we fail to template, it might still lex

        t1 = time.monotonic()
        bencher("Templating {0!r}".format(short_fname))

        if s:
            verbosity_logger("LEXING RAW ({0})".format(fname), verbosity=verbosity)
            # Lex the file and log any problems
            try:
                file_segment, lex_vs = FileSegment.from_raw(s, config=config or self.config)
                # We might just get the violations as a list
                violations += lex_vs
            except SQLLexError as err:
                violations.append(err)
                file_segment = None
        else:
            file_segment = None

        if file_segment:
            verbosity_logger(file_segment.stringify(), verbosity=verbosity)

        t2 = time.monotonic()
        bencher("Lexing {0!r}".format(short_fname))
        verbosity_logger("PARSING ({0})".format(fname), verbosity=verbosity)
        # Parse the file and log any problems
        if file_segment:
            try:
                # Make a parse context and parse
                context = self.get_parse_context()
                context.verbosity = verbosity or context.verbosity
                context.recurse = recurse or context.recurse
                parsed = file_segment.parse(parse_context=context)
            except SQLParseError as err:
                violations.append(err)
                parsed = None
            if parsed:
                verbosity_logger(frame_msg("Parsed Tree:"), verbosity=verbosity)
                verbosity_logger(parsed.stringify(), verbosity=verbosity)
        else:
            parsed = None

        t3 = time.monotonic()
        time_dict = {'templating': t1 - t0, 'lexing': t2 - t1, 'parsing': t3 - t2}
        bencher("Finish parsing {0!r}".format(short_fname))
        return parsed, violations, time_dict

    def lint_string(self, s, fname='<string input>', verbosity=0, fix=False, config=None):
        """Lint a string.

        Returns:
            :obj:`LintedFile`: an object representing that linted file.

        """
        # Before templating, we want to get a store of what's in the file
        # so we can compare later. We iterate character by character because
        # we don't want to miss anything.
        raw_buff = s

        # TODO: Tidy this up - it's a mess
        # Using the new parser, read the file object.
        parsed, vs, time_dict = self.parse_string(s=s, fname=fname, verbosity=verbosity, config=config)

        templ_buff = None
        fixed_buff = None
        if parsed:
            # Store the templated version
            templ_buff = parsed.raw

            # Now extract all the unparsable segments
            for unparsable in parsed.iter_unparsables():
                # No exception has been raised explicitly, but we still create one here
                # so that we can use the common interface
                vs.append(
                    SQLParseError(
                        "Found unparsable segment @L{0:03d}P{1:03d}: {2!r}".format(
                            unparsable.pos_marker.line_no,
                            unparsable.pos_marker.line_pos,
                            unparsable.raw[:20] + "..."),
                        segment=unparsable
                    )
                )
                if verbosity >= 2:
                    verbosity_logger("Found unparsable segment...", verbosity=verbosity)
                    verbosity_logger(unparsable.stringify(), verbosity=verbosity)

            t0 = time.monotonic()
            # At this point we should evaluate whether any parsing errors have occured
            if verbosity >= 2:
                verbosity_logger("LINTING ({0})".format(fname), verbosity=verbosity)

            # NOW APPLY EACH LINTER
            if fix:
                # If we're in fix mode, then we need to progressively call and reconstruct
                working = parsed
                # Keep a set of previous versions to catch infinite loops.
                previous_versions = {working.raw}
                linting_errors = []
                last_fixes = None
                while True:
                    changed = False
                    for crawler in self.get_ruleset(config=config or self.config):
                        # fixes should be a dict {} with keys edit, delete, create
                        # delete is just a list of segments to delete
                        # edit and create are list of tuples. The first element is the
                        # "anchor", the segment to look for either to edit or to insert BEFORE.
                        # The second is the element to insert or create.

                        lerrs, _, fixes, _ = crawler.crawl(working, fix=True)
                        linting_errors += lerrs
                        if fixes:
                            verbosity_logger("Applying Fixes: {0}".format(fixes), verbosity=verbosity)
                            if fixes == last_fixes:
                                raise RuntimeError(
                                    ("Fixes appear to not have been applied, they are "
                                     "the same as last time! {0}").format(
                                        fixes))

                            last_fixes = fixes
                            new_working, fixes = working.apply_fixes(fixes)

                            # Check for infinite loops
                            if new_working.raw not in previous_versions:
                                working = new_working
                                previous_versions.add(working.raw)
                                changed = True
                            else:
                                print(("WARNING: One fix for {0} not applied, it would re-cause "
                                       "a previously fixed error.").format(crawler.code))
                    if not changed:
                        # The file is clean :)
                        break
                # Set things up to return the altered version
                parsed = working
            else:
                # Just get the violations
                linting_errors = []
                for crawler in self.get_ruleset(config=config or self.config):
                    lerrs, _, _, _ = crawler.crawl(parsed)
                    linting_errors += lerrs

            # Update the timing dict
            t1 = time.monotonic()
            time_dict['linting'] = t1 - t0

            vs += linting_errors
            fixed_buff = parsed.raw

        # We process the ignore config here if appropriate
        if config:
            for violation in vs:
                violation.ignore_if_in(config.get('ignore'))

        file_mask = (raw_buff, templ_buff, fixed_buff)
        res = LintedFile(fname, vs, time_dict, parsed,
                         file_mask=file_mask)
        # Do the logging as appropriate (don't log if fixing...)
        if not fix:
            # This is the main command line output from linting.
            self.log(format_file_violations(fname, res.violations, verbose=verbosity))
        return res

    def paths_from_path(self, path):
        """Return a set of sql file paths from a potentially more ambigious path string."""
        if not os.path.exists(path):
            raise IOError("Specified path does not exist")

        if not os.path.isdir(path):
            return [path]

        # If it's a directory then expand the path!
        buffer = []
        for dirpath, _, filenames in os.walk(path):
            for fname in filenames:
                for ext in self.sql_exts:
                    # is it a sql file?
                    if fname.endswith(ext):
                        # join the paths and normalise
                        buffer.append(os.path.normpath(os.path.join(dirpath, fname)))
        return sorted(buffer)

    def lint_string_wrapped(self, string, fname='<string input>', verbosity=0, fix=False):
        """Lint strings directly."""
        result = LintingResult()
        linted_path = LintedPath(fname)
        linted_path.add(
            self.lint_string(string, fname=fname, verbosity=verbosity, fix=fix)
        )
        result.add(linted_path)
        return result

    def lint_path(self, path, verbosity=0, fix=False):
        """Lint a path."""
        linted_path = LintedPath(path)
        self.log(format_linting_path(path, verbose=verbosity))
        for fname in self.paths_from_path(path):
            config = self.config.make_child_from_path(fname)
            # Handle unicode issues gracefully
            with open(fname, 'r', encoding='utf8', errors='backslashreplace') as target_file:
                linted_path.add(
                    self.lint_string(target_file.read(), fname=fname,
                                     verbosity=verbosity,
                                     fix=fix, config=config))
        return linted_path

    def lint_paths(self, paths, verbosity=0, fix=False):
        """Lint an iterable of paths."""
        # If no paths specified - assume local
        if len(paths) == 0:
            paths = (os.getcwd(),)
        # Set up the result to hold what we get back
        result = LintingResult()
        for path in paths:
            # Iterate through files recursively in the specified directory (if it's a directory)
            # or read the file directly if it's not
            result.add(self.lint_path(path, verbosity=verbosity, fix=fix))
        return result

    def parse_path(self, path, verbosity=0, recurse=True):
        """Parse a path of sql files.

        NB: This a generator which will yield the result of each file
        within the path iteratively.
        """
        for fname in self.paths_from_path(path):
            self.log('=== [\u001b[30;1m{0}\u001b[0m] ==='.format(fname))
            config = self.config.make_child_from_path(fname)
            # Handle unicode issues gracefully
            with open(fname, 'r', encoding='utf8', errors='backslashreplace') as target_file:
                yield self.parse_string(target_file.read(), fname=fname, verbosity=verbosity,
                                        recurse=recurse, config=config)
