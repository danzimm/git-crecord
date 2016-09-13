# crecord.py
#
# Copyright 2008 Mark Edgington <edgimar@gmail.com>
#
# This software may be used and distributed according to the terms of
# the GNU General Public License, incorporated herein by reference.
#
# Much of this extension is based on Bryan O'Sullivan's record extension.

'''text-gui based change selection during commit or qrefresh'''
from mercurial.i18n import _
from . import util
import cStringIO
import errno
import os
import tempfile
import subprocess

import crpatch
import chunk_selector

def dorecord(ui, repo, commitfunc, *pats, **opts):
    def recordfunc(ui, repo, message, match, opts):
        """This is generic record driver.

        Its job is to interactively filter local changes, and accordingly
        prepare working dir into a state, where the job can be delegated to
        non-interactive commit command such as 'commit' or 'qrefresh'.

        After the actual job is done by non-interactive command, working dir
        state is restored to original.

        In the end we'll record interesting changes, and everything else will be
        left in place, so the user can continue his work.
        """

        git_args = ["git", "diff", "--binary"]

        if opts['cached']:
            git_args.append("--cached")

        if not opts['index']:
            git_args.append("HEAD")

        p = subprocess.Popen(git_args, stdout=subprocess.PIPE, close_fds=util.closefds)
        fp = p.stdout

        # 0. parse patch
        fromfiles = set()
        tofiles = set()

        chunks = crpatch.parsepatch(fp)
        for c in chunks:
            if isinstance(c, crpatch.header):
                fromfile, tofile = c.files()
                fromfiles.add(fromfile)
                tofiles.add(tofile)

        added = tofiles - fromfiles
        removed = fromfiles - tofiles
        modified = tofiles - added - removed
        changes = [modified, added, removed]

        # 1. filter patch, so we have intending-to apply subset of it
        chunks = crpatch.filterpatch(opts,
                                     chunks,
                                     chunk_selector.chunkselector, ui)
        p.wait()
        del fp

        contenders = set()
        for h in chunks:
            try:
                contenders.update(set(h.files()))
            except AttributeError:
                pass

        changed = changes[0] | changes[1] | changes[2]
        newfiles = [f for f in changed if f in contenders]

        if not newfiles:
            ui.status(_('no changes to record\n'))
            return 0


        # 2. backup changed files, so we can restore them in the end
        backups = {}
        newly_added_backups = {}
        backupdir = os.path.join(repo.controldir(), 'record-backups')
        try:
            os.mkdir(backupdir)
        except OSError, err:
            if err.errno != errno.EEXIST:
                raise
        index_backup = None
        try:
            index_backup = repo.open_index()

            # backup continues
            for f in newfiles:
                if f not in (modified | added):
                    continue
                fd, tmpname = tempfile.mkstemp(prefix=f.replace('/', '_')+'.',
                                               dir=backupdir)
                os.close(fd)
                ui.debug('backup %r as %r\n' % (f, tmpname))
                util.copyfile(os.path.join(repo.path, f), tmpname)
                if f in modified:
                    backups[f] = tmpname
                elif f in added:
                    newly_added_backups[f] = tmpname

            fp = cStringIO.StringIO()
            all_backups = {}
            all_backups.update(backups)
            all_backups.update(newly_added_backups)
            for c in chunks:
                if c.filename() in all_backups:
                    c.write(fp)
            dopatch = fp.tell()
            fp.seek(0)

            # 2.5 optionally review / modify patch in text editor
            if opts['crecord_reviewpatch']:
                patchtext = fp.read()
                reviewedpatch = ui.edit(patchtext, "")
                fp.truncate(0)
                fp.write(reviewedpatch)
                fp.seek(0)

            # 3a. apply filtered patch to clean repo  (clean)
            if backups:
                util.system(['git', 'checkout', '-f', '--'] + newfiles,
                       onerr=util.Abort, errprefix=_("checkout failed"))
            # remove newly added files from 'clean' repo (so patch can apply)
            for f in newly_added_backups:
                os.unlink(os.path.join(repo.path, f))

            # 3b. (apply)
            if dopatch:
                try:
                    ui.debug('applying patch\n')
                    ui.debug(fp.getvalue())
                    p = subprocess.Popen(["git", "apply"], stdin=subprocess.PIPE, close_fds=util.closefds)
                    p.stdin.write(fp.read())
                    p.stdin.close()
                    p.wait()
                except Exception as err:
                    s = str(err)
                    if s:
                        raise util.Abort(s)
                    else:
                        raise util.Abort(_('patch failed to apply'))
            del fp

            # 4. We prepared working directory according to filtered patch.
            #    Now is the time to delegate the job to commit/qrefresh or the like!

            # it is important to first chdir to repo root -- we'll call a
            # highlevel command with list of pathnames relative to repo root
            newfiles = [os.path.join(repo.path, n) for n in newfiles]
            ui.commit(*newfiles, **opts)
            index_backup = None

            return 0
        finally:
            # 5. finally restore backed-up files
            try:
                for realname, tmpname in backups.iteritems():
                    ui.debug('restoring %r to %r\n' % (tmpname, realname))
                    util.copyfile(tmpname, os.path.join(repo.path, realname))
                    os.unlink(tmpname)
                for realname, tmpname in newly_added_backups.iteritems():
                    ui.debug('restoring %r to %r\n' % (tmpname, realname))
                    util.copyfile(tmpname, os.path.join(repo.path, realname))
                    os.unlink(tmpname)
                os.rmdir(backupdir)
                if index_backup:
                    index_backup.write()
            except OSError, NameError:
                pass

    return recordfunc(ui, repo, "", None, opts)
