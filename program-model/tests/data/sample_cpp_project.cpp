/**
 * Sample C++ project for integration testing the Tree-sitter parsing pipeline.
 * Contains classes, methods, templates, and various C++ constructs.
 */

#include <cstdio>
#include <cstring>
#include <vector>

#define VERSION_MAJOR 1
#define VERSION_MINOR 0
#define MAKE_VERSION(major, minor) ((major << 16) | minor)

// Forward declarations
class Parser;

enum class LogLevel {
    DEBUG = 0,
    INFO = 1,
    WARNING = 2,
    ERROR = 3,
};

struct Config {
    int max_retries;
    double timeout;
    bool verbose;
};

class Logger {
private:
    LogLevel level;
    const char* prefix;

public:
    Logger(LogLevel lvl, const char* pfx) : level(lvl), prefix(pfx) {}

    void log(const char* msg) {
        printf("[%s] %s\n", prefix, msg);
    }

    LogLevel getLevel() const {
        return level;
    }

    void setLevel(LogLevel lvl) {
        level = lvl;
    }
};

class Parser {
private:
    char* buffer;
    size_t buffer_size;
    Logger logger;

public:
    Parser(size_t size) : buffer(nullptr), buffer_size(size), logger(LogLevel::INFO, "Parser") {
        buffer = new char[size];
    }

    ~Parser() {
        delete[] buffer;
    }

    int parse(const char* input, size_t len) {
        if (len > buffer_size) {
            logger.log("Input too large");
            return -1;
        }
        memcpy(buffer, input, len);
        return processBuffer(len);
    }

    int processBuffer(size_t len) {
        int result = 0;
        for (size_t i = 0; i < len; i++) {
            result += buffer[i];
        }
        return result;
    }

    size_t getBufferSize() const {
        return buffer_size;
    }
};

// Qualified name function definition
void Parser::log(const char* msg) {
    logger.log(msg);
}

// Free function
int computeChecksum(const char* data, size_t len) {
    int checksum = 0;
    for (size_t i = 0; i < len; i++) {
        checksum ^= data[i];
    }
    return checksum;
}

// Function with complex signature
static inline const char* getVersionString(void) {
    return "1.0.0";
}

#ifdef ENABLE_EXPERIMENTAL
int experimentalFeature(int x) {
    return x * 2;
}
#else
int experimentalFeature(int x) {
    return x;
}
#endif

int main() {
    Parser p(1024);
    int result = p.parse("hello", 5);
    printf("Result: %d\n", result);
    printf("Checksum: %d\n", computeChecksum("hello", 5));
    return 0;
}
