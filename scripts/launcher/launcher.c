// Native launcher for Meeting Recorder.app.
// It stays alive as the app's main process and runs Resources/launcher.sh as
// a child. macOS then attributes microphone access to "Meeting Recorder"
// (a script that exec()s into Python would be attributed to Apple's Python,
// which silently denies the microphone).
#include <mach-o/dyld.h>
#include <libgen.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;
static pid_t child = 0;

static void forward(int sig) { if (child > 0) kill(child, sig); }

int main(int argc, char *argv[]) {
    char exe[PATH_MAX], real[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    snprintf(script, sizeof(script), "%s/../Resources/launcher.sh", dirname(real));

    char *args[argc + 3];
    args[0] = "/bin/bash";
    args[1] = script;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;

    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    signal(SIGHUP, forward);
    if (posix_spawn(&child, "/bin/bash", NULL, NULL, args, environ) != 0) return 1;
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
