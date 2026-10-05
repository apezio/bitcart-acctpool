"""Process hardening (SPEC 3.6): no core dump, no ptrace/proc-mem access by the same uid, memory not in swap."""

import ctypes
import os
import resource

PR_SET_DUMPABLE = 4
MCL_CURRENT, MCL_FUTURE = 1, 2


def harden() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_DUMPABLE, 0) failed")
    os.umask(0o077)  # keystore, journal and audit log are for the owner only


def lock_memory() -> bool:
    """mlockall, only with an unlimited RLIMIT_MEMLOCK (else a later allocation fails). True when locked."""
    if resource.getrlimit(resource.RLIMIT_MEMLOCK)[0] != resource.RLIM_INFINITY:
        return False
    return ctypes.CDLL(None, use_errno=True).mlockall(MCL_CURRENT | MCL_FUTURE) == 0
