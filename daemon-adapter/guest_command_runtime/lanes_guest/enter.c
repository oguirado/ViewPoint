/* Final credential drop inside a root-prepared bubblewrap namespace. */
#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <unistd.h>

#if defined(__aarch64__)
#define LANE_ARCH AUDIT_ARCH_AARCH64
#elif defined(__x86_64__)
#define LANE_ARCH AUDIT_ARCH_X86_64
#else
#error unsupported lane seccomp ABI
#endif

#define DENY(error) (SECCOMP_RET_ERRNO | (error))
#define BLOCK(syscall, error) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_##syscall, 0, 1), \
    BPF_STMT(BPF_RET | BPF_K, DENY(error))
#define LOAD_ARGUMENT(index) \
    BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[index]))
#define NAMESPACES (CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWIPC | \
                    CLONE_NEWPID | CLONE_NEWUTS | CLONE_NEWCGROUP | CLONE_NEWTIME)

static int install_filter(void)
{
    static struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, LANE_ARCH, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JGE | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, DENY(ENOSYS)),
#endif
        BLOCK(ptrace, EPERM),
        BLOCK(process_vm_readv, EPERM),
        BLOCK(process_vm_writev, EPERM),
        BLOCK(pidfd_getfd, EPERM),
        BLOCK(kcmp, EPERM),
        BLOCK(unshare, EPERM),
        BLOCK(setns, EPERM),
        BLOCK(mount, EPERM),
        BLOCK(umount2, EPERM),
        BLOCK(pivot_root, EPERM),
        BLOCK(open_tree, EPERM),
        BLOCK(move_mount, EPERM),
        BLOCK(mount_setattr, EPERM),
        BLOCK(fsopen, EPERM),
        BLOCK(fsconfig, EPERM),
        BLOCK(fsmount, EPERM),
        BLOCK(open_by_handle_at, EPERM),
        BLOCK(io_uring_setup, EPERM),
        BLOCK(io_uring_register, EPERM),
        BLOCK(io_uring_enter, EPERM),
        BLOCK(bpf, EPERM),
        BLOCK(perf_event_open, EPERM),
        BLOCK(clone3, ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 4),
        LOAD_ARGUMENT(0),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, NAMESPACES, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, DENY(EPERM)),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_socket, 1, 0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_socketpair, 0, 3),
        LOAD_ARGUMENT(0),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, AF_UNIX, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, DENY(EPERM)),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {sizeof(filter) / sizeof(filter[0]), filter};
    return prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program);
}

int main(int argc, char *argv[])
{
    uid_t uid;
    if (argc < 3 || geteuid() != 0)
        return 126;
    if (strcmp(argv[1], "0") == 0)
        uid = 10000;
    else if (strcmp(argv[1], "1") == 0)
        uid = 10001;
    else
        return 126;

    struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
    struct __user_cap_data_struct capabilities[2] = {{0}};
    if (setgroups(0, NULL) || prctl(PR_SET_KEEPCAPS, 0L, 0L, 0L, 0L) ||
        setresgid(uid, uid, uid) || setresuid(uid, uid, uid) ||
        syscall(SYS_capset, &header, &capabilities) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1L, 0L, 0L, 0L) ||
        syscall(SYS_close_range, 3U, ~0U, 0U) || install_filter()) {
        perror("lane credential or seccomp setup");
        return 126;
    }
    execv(argv[2], argv + 2);
    perror("lane command exec");
    return 126;
}
