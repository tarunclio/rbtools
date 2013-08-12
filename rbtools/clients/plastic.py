import logging
import os
import re

from rbtools.clients import SCMClient, RepositoryInfo
from rbtools.utils.checks import check_install
from rbtools.utils.filesystem import make_tempfile
from rbtools.utils.process import die, execute


class PlasticClient(SCMClient):
    """
    A wrapper around the cm Plastic tool that fetches repository
    information and generates compatible diffs
    """
    name = 'Plastic'

    def __init__(self, **kwargs):
        super(PlasticClient, self).__init__(**kwargs)

    def get_repository_info(self):
        if not check_install('cm version'):
            return None

        # Get the workspace directory, so we can strip it from the diff output
        self.workspacedir = execute(["cm", "gwp", ".", "--format={1}"],
                                    split_lines=False,
                                    ignore_errors=True).strip()

        logging.debug("Workspace is %s" % self.workspacedir)

        # Get the repository that the current directory is from
        split = execute(["cm", "ls", self.workspacedir, "--format={8}"], split_lines=True,
                        ignore_errors=True)

        # remove blank lines
        split = filter(None, split)

        m = re.search(r'^rep:(.+)$', split[0], re.M)

        if not m:
            return None

        path = m.group(1)

        return RepositoryInfo(path,
                              supports_changesets=True,
                              supports_parent_diffs=False)

    def get_changenum(self, args):
        """ Extract the integer value from a changeset ID (cs:1234) """
        if len(args) == 1 and args[0].startswith("cs:"):
                try:
                    return str(int(args[0][3:]))
                except ValueError:
                    pass

        return None

    def sanitize_changenum(self, changenum):
        """ Return a "sanitized" change number.  Currently a no-op """
        return changenum

    def diff(self, args):
        """
        Performs a diff across all modified files in a Plastic workspace

        Parent diffs are not supported (the second value in the tuple).
        """
        changenum = self.get_changenum(args)

        if changenum is None:
            diff = self.branch_diff(args)
        else:
            diff = self.changenum_diff(changenum)

        return {
            'diff': diff,
        }

    def diff_between_revisions(self, revision_range, args, repository_info):
        """
        This doesn't make much sense in Plastic SCM 4.
        We'll only implement revisions for changests and branches
        """
        die("This option is not supported. Only reviews of a changeset or branch are supported")

    def branch_diff(self, args):
        logging.debug("branch diff: %s" % (args))

        if len(args) > 0:
            branch = args[0]
        else:
            branch = args

        if not getattr(self.options, 'branch', None):
            self.options.branch = branch

        diff_entries = execute(["cm", "diff", branch,
                         "--format={status} {path} "
                         "rev:revid:{revid} rev:revid:{parentrevid} "
                         "src:{srccmpath} "
                         "dst:{dstcmpath}{newline}"],
                         split_lines = True)

        logging.debug("got files: %s" % (diff_entries))
        return self.process_diffs(diff_entries)


    def changenum_diff(self, changenum):
        logging.debug("changenum_diff: %s" % (changenum))

        diff_entries = execute(["cm", "diff", "cs:" + changenum,
                         "--format={status} {path} "
                         "rev:revid:{revid} rev:revid:{parentrevid} "
                         "src:{srccmpath} "
                         "dst:{dstcmpath}{newline}"],
                         split_lines = True)

        logging.debug("got files: %s" % (diff_entries))
        return self.process_diffs(diff_entries)

    def process_diffs(self, my_diff_entries):
        # Diff generation based on perforce client
        diff_lines = []

        empty_filename = make_tempfile()
        tmp_diff_from_filename = make_tempfile()
        tmp_diff_to_filename = make_tempfile()

        for f in my_diff_entries:
            f = f.strip()

            if not f:
                continue

            m = re.search(r'(?P<type>[ACMD]) (?P<file>.*) '
                          r'(?P<revspec>rev:revid:[-\d]+) '
                          r'(?P<parentrevspec>rev:revid:[-\d]+) '
                          r'src:(?P<srcpath>.*) '
                          r'dst:(?P<dstpath>.*)$',
                          f)
            if not m:
                die("Could not parse 'cm log' response: %s" % f)

            changetype = m.group("type")
            filename = m.group("file")

            if changetype == "M":
                # Handle moved files as a delete followed by an add.
                # Clunky, but at least it works
                oldfilename = m.group("srcpath")
                oldspec = m.group("revspec")
                newfilename = m.group("dstpath")
                newspec = m.group("revspec")

                self.write_file(oldfilename, oldspec, tmp_diff_from_filename)
                dl = self.diff_files(tmp_diff_from_filename, empty_filename,
                                    oldfilename, "rev:revid:-1", oldspec,
                                    changetype)
                diff_lines += dl

                self.write_file(newfilename, newspec, tmp_diff_to_filename)
                dl = self.diff_files(empty_filename, tmp_diff_to_filename,
                                    newfilename, newspec, "rev:revid:-1",
                                    changetype)
                diff_lines += dl

            else:
                newrevspec = m.group("revspec")
                parentrevspec = m.group("parentrevspec")

                logging.debug("Type %s File %s Old %s New %s" % (changetype,
                                                         filename,
                                                         parentrevspec,
                                                         newrevspec))

                old_file = new_file = empty_filename

                if (changetype in ['A'] or
                    (changetype in ['C'] and
                    parentrevspec == "rev:revid:-1")):
                    # There's only one content to show
                    self.write_file(filename, newrevspec, tmp_diff_to_filename)
                    new_file = tmp_diff_to_filename
                elif changetype in ['C']:
                    self.write_file(filename, parentrevspec,
                                tmp_diff_from_filename)
                    old_file = tmp_diff_from_filename
                    self.write_file(filename, newrevspec, tmp_diff_to_filename)
                    new_file = tmp_diff_to_filename
                elif changetype in ['D']:
                    self.write_file(filename, parentrevspec,
                                tmp_diff_from_filename)
                    old_file = tmp_diff_from_filename
                else:
                    die("Don't know how to handle change type '%s' for %s" %
                        (changetype, filename))

                dl = self.diff_files(old_file, new_file, filename,
                                 newrevspec, parentrevspec, changetype)
                diff_lines += dl

        os.unlink(empty_filename)
        os.unlink(tmp_diff_from_filename)
        os.unlink(tmp_diff_to_filename)

        return ''.join(diff_lines)

    def diff_files(self, old_file, new_file, filename, newrevspec,
                   parentrevspec, changetype):
        """
        Do the work of producing a diff for Plastic (based on the Perforce one)

        old_file - The absolute path to the "old" file.
        new_file - The absolute path to the "new" file.
        filename - The file in the Plastic workspace
        newrevspec - The revid spec of the changed file
        parentrevspecspec - The revision spec of the "old" file
        changetype - The change type as a single character string

        Returns a list of strings of diff lines.
        """
        if filename.startswith(self.workspacedir):
            filename = filename[len(self.workspacedir):]

        diff_cmd = ["diff", "-urN", old_file, new_file]
        # Diff returns "1" if differences were found.
        dl = execute(diff_cmd, extra_ignore_errors=(1,2),
                     translate_newlines = False)

        # If the input file has ^M characters at end of line, lets ignore them.
        dl = dl.replace('\r\r\n', '\r\n')
        dl = dl.splitlines(True)

        # Special handling for the output of the diff tool on binary files:
        #     diff outputs "Files a and b differ"
        # and the code below expects the output to start with
        #     "Binary files "
        if (len(dl) == 1 and
            dl[0].startswith('Files %s and %s differ' % (old_file, new_file))):
            dl = ['Binary files %s and %s differ\n' % (old_file, new_file)]

        if dl == [] or dl[0].startswith("Binary files "):
            if dl == []:
                return []

            dl.insert(0, "==== %s (%s) ==%s==\n" % (filename, newrevspec,
                                                    changetype))
            dl.append('\n')
        else:
            dl[0] = "--- %s\t%s\n" % (filename, parentrevspec)
            dl[1] = "+++ %s\t%s\n" % (filename, newrevspec)

            # Not everybody has files that end in a newline.  This ensures
            # that the resulting diff file isn't broken.
            if dl[-1][-1] != '\n':
                dl.append('\n')

        return dl

    def write_file(self, filename, filespec, tmpfile):
        """ Grabs a file from Plastic and writes it to a temp file """
        logging.debug("Writing '%s' (rev %s) to '%s'" % (filename, filespec, tmpfile))
        execute(["cm", "cat", filespec, "--file=" + tmpfile])

