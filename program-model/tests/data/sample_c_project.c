/**
 * Sample C project for integration testing the Tree-sitter parsing pipeline.
 * Contains various function patterns, type definitions, and preprocessor directives.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Type definitions */
#define MAX_BUFFER_SIZE 1024
#define MIN(a, b) ((a) < (b) ? (a) : (b))

typedef unsigned int uint32;

struct buffer {
    char *data;
    size_t size;
    size_t capacity;
};

union value {
    int i;
    float f;
    char c;
};

enum status {
    STATUS_OK = 0,
    STATUS_ERROR = 1,
    STATUS_PENDING = 2,
};

typedef struct {
    int x;
    int y;
} point_t;

/* Forward declarations */
struct forward_only;

/**
 * Initialize a buffer with the given capacity.
 */
int buffer_init(struct buffer *buf, size_t capacity) {
    buf->data = (char *)malloc(capacity);
    if (buf->data == NULL) {
        return STATUS_ERROR;
    }
    buf->size = 0;
    buf->capacity = capacity;
    return STATUS_OK;
}

/* Write data to a buffer */
int buffer_write(struct buffer *buf, const char *data, size_t len) {
    if (buf->size + len > buf->capacity) {
        return STATUS_ERROR;
    }
    memcpy(buf->data + buf->size, data, len);
    buf->size += len;
    return STATUS_OK;
}

void buffer_free(struct buffer *buf) {
    free(buf->data);
    buf->data = NULL;
    buf->size = 0;
    buf->capacity = 0;
}

#ifdef ENABLE_COMPRESSION
int buffer_compress(struct buffer *buf) {
    /* Compression implementation */
    return STATUS_OK;
}
#else
int buffer_compress(struct buffer *buf) {
    /* No-op when compression is disabled */
    return STATUS_ERROR;
}
#endif

/* Static helper function */
static int validate_input(const char *input, size_t len) {
    if (input == NULL || len == 0) {
        return 0;
    }
    return 1;
}

/* Function with complex pointer return type */
const char *buffer_get_data(const struct buffer *buf) {
    return buf->data;
}

/* Nested preprocessor directives */
#ifdef PLATFORM_LINUX
#  ifdef ARCH_X86
int platform_specific_func(void) {
    return 1;
}
#  else
int platform_specific_func(void) {
    return 2;
}
#  endif
#endif

int main(int argc, char *argv[]) {
    struct buffer buf;
    buffer_init(&buf, MAX_BUFFER_SIZE);
    buffer_write(&buf, "hello", 5);
    printf("Data: %s\n", buffer_get_data(&buf));
    buffer_free(&buf);
    return 0;
}
