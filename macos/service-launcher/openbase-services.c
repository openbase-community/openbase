/*
 * Openbase Services launcher.
 *
 * The launchd job for every Openbase Coder service on macOS starts this
 * executable, which runs the real service (the generated wrapper script that
 * execs the bundled Python, livekit-server, openbase-tunneld, ...) as a child
 * and stays alive as the job's process for the child's whole life.
 *
 * Why a launcher instead of exec'ing the service directly: macOS attributes
 * TCC decisions (Desktop/Documents folder access, Local Network, ...) to the
 * *responsible* process, and launchd makes the job's own process responsible
 * for itself and every descendant it spawns. With the Python interpreter as
 * the job process, TCC keyed each grant on an ad-hoc-signed binary under a
 * versioned release directory, so every self-update produced a new client and
 * re-prompted the user. This launcher lives in a signed app bundle with a
 * fixed bundle identifier, so the grant is keyed on that stable identity and
 * survives runtime updates (see dev-docs/MACOS_SERVICE_IDENTITY.md).
 *
 * Behaviour contract (covered by cli/tests/test_service_launcher.py):
 *   - argv[1..] is the child command; it is spawned with the launcher's
 *     environment, working directory, file descriptors and process group.
 *   - Termination signals received by the launcher (SIGTERM, SIGINT, SIGHUP,
 *     SIGQUIT, SIGUSR1, SIGUSR2) are forwarded to the child.
 *   - The launcher exits with the child's exit status; when the child dies by
 *     a signal, the launcher re-raises that signal on itself so launchd sees
 *     the same termination reason.
 *   - A failed spawn exits 127 after printing the error.
 */

#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static volatile pid_t child_pid = 0;

static const int forwarded_signals[] = {
    SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1, SIGUSR2,
};

static void forward_signal(int signo) {
    pid_t pid = child_pid;
    if (pid > 0) {
        kill(pid, signo);
    }
}

static int install_forwarders(void) {
    struct sigaction action;
    memset(&action, 0, sizeof(action));
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    action.sa_flags = SA_RESTART;
    for (size_t i = 0; i < sizeof(forwarded_signals) / sizeof(forwarded_signals[0]); i++) {
        if (sigaction(forwarded_signals[i], &action, NULL) != 0) {
            return -1;
        }
    }
    return 0;
}

static void reset_forwarders(void) {
    for (size_t i = 0; i < sizeof(forwarded_signals) / sizeof(forwarded_signals[0]); i++) {
        signal(forwarded_signals[i], SIG_DFL);
    }
}

int main(int argc, char *argv[]) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <command> [args...]\n", argv[0]);
        return 64;
    }

    if (install_forwarders() != 0) {
        perror("openbase-services: sigaction");
        return 70;
    }

    posix_spawnattr_t attributes;
    posix_spawnattr_init(&attributes);
    /* The child starts with default signal dispositions and an empty mask,
     * whatever launchd or the shell handed the launcher. */
    sigset_t all_signals;
    sigfillset(&all_signals);
    sigset_t no_signals;
    sigemptyset(&no_signals);
    posix_spawnattr_setsigdefault(&attributes, &all_signals);
    posix_spawnattr_setsigmask(&attributes, &no_signals);
    posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_SETSIGMASK);

    pid_t pid = 0;
    int spawn_error = posix_spawnp(&pid, argv[1], NULL, &attributes, &argv[1], environ);
    posix_spawnattr_destroy(&attributes);
    if (spawn_error != 0) {
        fprintf(stderr, "openbase-services: cannot start %s: %s\n", argv[1], strerror(spawn_error));
        return 127;
    }
    child_pid = pid;

    int status = 0;
    for (;;) {
        pid_t waited = waitpid(pid, &status, 0);
        if (waited == pid) {
            break;
        }
        if (waited < 0 && errno == EINTR) {
            continue;
        }
        perror("openbase-services: waitpid");
        return 70;
    }
    child_pid = 0;

    if (WIFSIGNALED(status)) {
        int signo = WTERMSIG(status);
        reset_forwarders();
        raise(signo);
        /* Only reached when the signal is ignored or blocked. */
        return 128 + signo;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    return 70;
}
