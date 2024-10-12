#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import os
import time
import platform
import tempfile

from gunicorn import util

PLATFORM = platform.system()
IS_CYGWIN = PLATFORM.startswith('CYGWIN')


class WorkerTmp:
    """A class to manage temporary files for workers."""

    def __init__(self, cfg):
        # gaojian: os.umask(mode) 设置当前进程的文件创建模式掩码(umask)，并返回之前的文件创建模式掩码(umask)。
        # gaojian: 文件模式创建掩码是一个8进制数(比如0o755)，它确定了在创建新文件或目录时所使用的默认权限。
        old_umask = os.umask(cfg.umask)
        fdir = cfg.worker_tmp_dir
        if fdir and not os.path.isdir(fdir):
            raise RuntimeError("%s doesn't exist. Can't create workertmp." % fdir)
        fd, name = tempfile.mkstemp(prefix="wgunicorn-", dir=fdir)
        os.umask(old_umask)

        # change the owner and group of the file if the worker will run as
        # a different user or group, so that the worker can modify the file
        if cfg.uid != os.geteuid() or cfg.gid != os.getegid():
            util.chown(name, cfg.uid, cfg.gid)

        # unlink the file so we don't leak temporary files
        # we will keep the file open to keep the file descriptor
        # alive and use it to notify the worker of changes
        # to the file
        # gaojian: 删除文件，以防泄漏临时文件
        # gaojian: 我们将保持文件打开以保持文件描述符存活，并使用它来通知工作进程文件的更改。

        # gaojian: 在Linux系统中，删除文件后，文件的内容仍然存在，只是文件的目录项被删除了(无法通过文件名查找到该文件了)，
        # gaojian: 但是文件的内容还存在，当所有已打开的文件描述符被关闭后才会被真正删除；
        try:
            if not IS_CYGWIN:
                util.unlink(name)
            # In Python 3.8, open() emits RuntimeWarning if buffering=1 for binary mode.
            # Because we never write to this file, pass 0 to switch buffering off.

            # gaojian: 在Python 3.8中，open()方法以二进制模式打开文件时，如果buffering= 1，会发出RuntimeWarning。
            # gaojian: 因为buffering=1表示行缓冲，行缓冲仅适用于文本模式下，二进制模式下没有行的概念；
            # gaojian: 由于我们从不写入此文件，所以传递0以关闭缓冲；

            # gaojian: buffering=0 表示无缓冲；
            # gaojian: buffering=1 表示行缓冲，行缓冲仅适用于文本模式下，二进制模式下没有行的概念；
            # gaojian: buffering>1 表示缓冲区大小，即缓冲区大小为buffering字节；
            # gaojian: 示例：
            # gaojian: with open('example.bin', 'wb', buffering=4096) as file:
            # gaojian:     file.write(b'Hello, world!')
            self._tmp = os.fdopen(fd, 'w+b', 0)
        except Exception:
            os.close(fd)
            raise

    def notify(self):
        """设置文件的访问和修改时间"""
        new_time = time.monotonic()
        # gaojian: os.utime(path, times=None, *, ns=None, dir_fd=None, follow_symlinks=True)
        # gaojian: times 一个二元组 (atime, mtime)，分别表示访问时间和修改时间。可以是浮点数或整数（表示秒）
        os.utime(self._tmp.fileno(), (new_time, new_time))

    def last_update(self):
        """返回文件的最后修改时间"""
        return os.fstat(self._tmp.fileno()).st_mtime

    def fileno(self):
        """返回文件描述符"""
        return self._tmp.fileno()

    def close(self):
        """关闭文件"""
        return self._tmp.close()
