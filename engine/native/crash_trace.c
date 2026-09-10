/* Best-effort, bounded native fault evidence. No core, registers, locals or memory dump.
 * The supervisor opens the owner-only descriptor before exec. An alarm bounds unwinding
 * even if a fault interrupted the loader; the fault PC is emitted before stack unwinding.
 * Object-relative offsets remain useful with the exact image's stripped ELF build IDs.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <execinfo.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <ucontext.h>
#include <unistd.h>

static int trace_fd = -1;
static volatile sig_atomic_t original_signal;
static char alternate_stack[65536];

static void finish(int ignored)
{
    int sig = original_signal ? original_signal : SIGABRT;
    sigset_t mask;
    (void)ignored;
    signal(sig, SIG_DFL);
    sigemptyset(&mask);
    sigaddset(&mask, sig);
    sigprocmask(SIG_UNBLOCK, &mask, NULL);
    raise(sig);
    _exit(128 + sig);
}

static void safe_name(char *out, const char *in, size_t size)
{
    size_t i = 0;
    if (!in) in = "unknown";
    for (; i + 1 < size && in[i]; i++) {
        unsigned char c = in[i];
        out[i] = ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                  (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '-') ? c : '_';
    }
    out[i] = 0;
}

static void frame(int index, void *pc)
{
    Dl_info info = {0};
    char object[96], function[128], line[384];
    dladdr(pc, &info);
    const char *name = info.dli_fname ? strrchr(info.dli_fname, '/') : NULL;
    safe_name(object, name ? name + 1 : info.dli_fname, sizeof(object));
    safe_name(function, info.dli_sname, sizeof(function));
    unsigned long offset = info.dli_fbase ? (uintptr_t)pc - (uintptr_t)info.dli_fbase : 0;
    int length = snprintf(line, sizeof(line), "frame=%d object=%s offset=%lx function=%s\n",
                          index, object, offset, function);
    if (length > 0 && length < (int)sizeof(line) && write(trace_fd, line, length) != length) finish(0);
}

static void fault(int sig, siginfo_t *info, void *context)
{
    void *frames[32];
    char header[64];
    (void)info;
    original_signal = sig;
    signal(SIGALRM, finish);
    alarm(2);
    int length = snprintf(header, sizeof(header), "MDD_CRASH signal=%d\n", sig);
    if (write(trace_fd, header, length) != length) finish(0);
#if defined(__x86_64__)
    frame(0, (void *)((ucontext_t *)context)->uc_mcontext.gregs[REG_RIP]);
#endif
    int count = backtrace(frames, 32);
    for (int i = 0; i < count; i++) frame(i + 1, frames[i]);
    if (write(trace_fd, "MDD_CRASH_END\n", 14) != 14) finish(0);
    finish(sig);
}

__attribute__((constructor)) static void install_trace(void)
{
    const char *value = getenv("MDD_CRASH_FD");
    if (!value) return;
    char *end = NULL;
    long descriptor = strtol(value, &end, 10);
    if (!end || *end || descriptor < 3 || descriptor > 65535) return;
    trace_fd = descriptor;
    fcntl(trace_fd, F_SETFD, FD_CLOEXEC);
    unsetenv("LD_PRELOAD");
    unsetenv("MDD_CRASH_FD");
    struct rlimit limit = {0, 0};
    setrlimit(RLIMIT_CORE, &limit);
    prctl(PR_SET_DUMPABLE, 0); /* also prevents the host's piped core handler collecting secrets */
    void *warmup[1];
    backtrace(warmup, 1); /* load the unwinder before entering a signal handler */
    stack_t stack = {.ss_sp = alternate_stack, .ss_size = sizeof(alternate_stack)};
    sigaltstack(&stack, NULL);
    struct sigaction action = {.sa_sigaction = fault, .sa_flags = SA_SIGINFO | SA_ONSTACK | SA_RESETHAND};
    sigemptyset(&action.sa_mask);
    int signals[] = {SIGSEGV, SIGABRT, SIGBUS, SIGILL, SIGFPE};
    for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); i++) sigaction(signals[i], &action, NULL);
}
