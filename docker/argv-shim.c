/*
 * ambient-streamer — keeps the YouTube stream key out of /proc/<pid>/cmdline.
 *
 * FFmpeg takes -rtmp_playpath from argv, and /proc/<pid>/cmdline is world
 * readable, so `ps` on the host would show the stream key of every channel.
 * FFmpeg has no way to read an AVOption from a file (-/opt is rejected for
 * AVOptions), so the substitution has to happen inside the process.
 *
 * publish-youtube.sh launches ffmpeg with the literal token below in argv and
 * this object preloaded. The constructor runs before main(), so it can repoint
 * that argv slot at a heap copy of the real value: ffmpeg then parses the
 * secret, while the kernel's cmdline region — the only thing /proc exposes —
 * still holds the token.
 *
 * Fails closed. If anything is missing or unexpected the token is left in
 * place, ffmpeg publishes to a playpath YouTube rejects, and nothing leaks.
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define AMBIENT_TOKEN "@AMBIENT_PLAYPATH@"
#define AMBIENT_MAX_SECRET 512

extern char **environ;

static long argv_count(void)
{
	char buf[4096];
	long argc = 0;
	ssize_t n;
	int fd;

	fd = open("/proc/self/cmdline", O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	while ((n = read(fd, buf, sizeof buf)) > 0) {
		ssize_t i;

		for (i = 0; i < n; i++)
			if (buf[i] == '\0')
				argc++;
	}
	close(fd);
	return n < 0 ? -1 : argc;
}

static char *read_secret(const char *path)
{
	char *buf;
	ssize_t n;
	int fd;

	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return NULL;
	buf = calloc(1, AMBIENT_MAX_SECRET + 1);
	if (!buf) {
		close(fd);
		return NULL;
	}
	n = read(fd, buf, AMBIENT_MAX_SECRET);
	close(fd);
	if (n <= 0) {
		free(buf);
		return NULL;
	}
	buf[strcspn(buf, "\r\n")] = '\0';
	if (buf[0] == '\0') {
		free(buf);
		return NULL;
	}
	return buf;
}

__attribute__((constructor))
static void ambient_argv_substitute(void)
{
	const char *path = getenv("AMBIENT_PLAYPATH_FILE");
	char **argv;
	char *secret;
	long argc, i;

	if (!path || !*path)
		return;

	argc = argv_count();
	if (argc <= 0)
		return;

	/* Kernel stack layout: argv[0..argc-1], NULL, environ[0..], NULL. */
	argv = environ - argc - 1;
	/* If that arithmetic is wrong this is not the NULL terminator — bail out. */
	if (argv[argc] != NULL || argv[0] == NULL)
		return;

	secret = read_secret(path);
	if (!secret)
		return;

	for (i = 0; i < argc; i++) {
		if (strcmp(argv[i], AMBIENT_TOKEN) == 0) {
			argv[i] = secret;
			return;
		}
	}

	memset(secret, 0, strlen(secret));
	free(secret);
}
