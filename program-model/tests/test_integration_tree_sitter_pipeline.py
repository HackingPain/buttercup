"""Integration tests for the Tree-sitter parsing pipeline.

These tests exercise the actual Tree-sitter parsing logic end-to-end using
real code snippets. They do not require external services (Redis, Docker, etc.).
"""

from pathlib import Path

import pytest

from buttercup.common.challenge_task import ChallengeTask
from buttercup.common.task_meta import TaskMeta
from buttercup.program_model.api.tree_sitter import (
    CodeTS,
    TypeDefinitionType,
)
from buttercup.program_model.utils.common import Function, FunctionBody

DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Fixtures – lightweight challenge tasks backed by the sample data files
# ---------------------------------------------------------------------------


def _make_challenge_task(tmp_path: Path, language: str, source_files: dict[str, str]) -> ChallengeTask:
    """Helper to build a minimal ChallengeTask with the given source files."""
    base_path = tmp_path / "task_rw"
    oss_fuzz = base_path / "fuzz-tooling" / "fuzz-tooling"
    source = base_path / "src" / "sample_project"

    oss_fuzz.mkdir(parents=True, exist_ok=True)
    source.mkdir(parents=True, exist_ok=True)

    # project.yaml
    project_yaml_path = oss_fuzz / "projects" / "sample_project" / "project.yaml"
    project_yaml_path.parent.mkdir(parents=True, exist_ok=True)
    project_yaml_path.write_text(f"language: {language}\n")

    # helper.py
    helper_path = oss_fuzz / "infra" / "helper.py"
    helper_path.parent.mkdir(parents=True, exist_ok=True)
    helper_path.write_text("import sys; sys.exit(0)\n")

    # Write source files
    for name, content in source_files.items():
        (source / name).write_text(content)

    # Task metadata
    TaskMeta(
        project_name="sample_project",
        focus="sample_project",
        task_id="task-id-integration",
        metadata={
            "task_id": "task-id-integration",
            "round_id": "testing",
            "team_id": "tob",
        },
    ).save(base_path)

    return ChallengeTask(read_only_task_dir=base_path)


@pytest.fixture
def c_challenge_task(tmp_path: Path) -> ChallengeTask:
    """ChallengeTask backed by the sample C project."""
    source_content = DATA_DIR.joinpath("sample_c_project.c").read_text()
    return _make_challenge_task(tmp_path, "c", {"sample.c": source_content})


@pytest.fixture
def cpp_challenge_task(tmp_path: Path) -> ChallengeTask:
    """ChallengeTask backed by the sample C++ project."""
    source_content = DATA_DIR.joinpath("sample_cpp_project.cpp").read_text()
    return _make_challenge_task(tmp_path, "cpp", {"sample.cpp": source_content})


@pytest.fixture
def java_challenge_task(tmp_path: Path) -> ChallengeTask:
    """ChallengeTask backed by the sample Java project."""
    source_content = DATA_DIR.joinpath("sample_java_project.java").read_text()
    return _make_challenge_task(tmp_path, "java", {"Sample.java": source_content})


# ---------------------------------------------------------------------------
# C parsing pipeline integration tests
# ---------------------------------------------------------------------------


class TestCParsingPipeline:
    """End-to-end tests for C code parsing through CodeTS."""

    def test_finds_all_expected_functions(self, c_challenge_task: ChallengeTask):
        """Verify that the parser discovers all top-level function definitions."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.c"))

        expected_names = {
            "buffer_init",
            "buffer_write",
            "buffer_free",
            "buffer_compress",
            "validate_input",
            "buffer_get_data",
            "main",
        }
        # platform_specific_func is behind nested #ifdef so might or might not appear
        # depending on preprocessor handling -- we only assert the guaranteed set
        assert expected_names.issubset(set(functions.keys())), (
            f"Missing functions: {expected_names - set(functions.keys())}"
        )

    def test_function_body_content(self, c_challenge_task: ChallengeTask):
        """Verify that function bodies contain the expected source code."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.c"))

        buf_init = functions["buffer_init"]
        assert len(buf_init.bodies) >= 1
        assert "malloc(capacity)" in buf_init.bodies[0].body
        assert "buf->data" in buf_init.bodies[0].body
        assert "STATUS_ERROR" in buf_init.bodies[0].body

    def test_function_line_numbers(self, c_challenge_task: ChallengeTask):
        """Verify that start/end line numbers are sensible and ordered."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.c"))

        for name, func in functions.items():
            for body in func.bodies:
                assert body.start_line > 0, f"{name}: start_line must be positive"
                assert body.end_line >= body.start_line, f"{name}: end_line must be >= start_line"

    def test_ifdef_produces_multiple_bodies(self, c_challenge_task: ChallengeTask):
        """Functions defined in both #ifdef and #else branches should yield two bodies."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.c"))

        compress_fn = functions.get("buffer_compress")
        assert compress_fn is not None, "buffer_compress should be found"
        assert len(compress_fn.bodies) == 2, "Expected 2 bodies for buffer_compress (#ifdef/#else)"

        # One body should reference compression, the other should be a no-op
        bodies_text = [b.body for b in compress_fn.bodies]
        assert any("Compression implementation" in b for b in bodies_text)
        assert any("No-op" in b for b in bodies_text)

    def test_get_function_by_name(self, c_challenge_task: ChallengeTask):
        """get_function should return a specific function by name and file."""
        code_ts = CodeTS(c_challenge_task)
        func = code_ts.get_function("buffer_write", Path("src/sample_project/sample.c"))

        assert func is not None
        assert func.name == "buffer_write"
        assert "memcpy" in func.bodies[0].body

    def test_get_function_nonexistent(self, c_challenge_task: ChallengeTask):
        """get_function should return None for a nonexistent function."""
        code_ts = CodeTS(c_challenge_task)
        func = code_ts.get_function("does_not_exist", Path("src/sample_project/sample.c"))
        assert func is None

    def test_parse_type_definitions(self, c_challenge_task: ChallengeTask):
        """Verify extraction of struct, union, enum, typedef, and preprocessor defs."""
        code_ts = CodeTS(c_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/sample.c"))

        # Struct
        assert "buffer" in types
        assert types["buffer"].type == TypeDefinitionType.STRUCT
        assert "char *data" in types["buffer"].definition

        # Union
        assert "value" in types
        assert types["value"].type == TypeDefinitionType.UNION
        assert "float f" in types["value"].definition

        # Enum
        assert "status" in types
        assert types["status"].type == TypeDefinitionType.ENUM
        assert "STATUS_OK" in types["status"].definition

        # Typedef
        assert "point_t" in types
        assert types["point_t"].type == TypeDefinitionType.TYPEDEF

        # Preprocessor type defs
        assert "MAX_BUFFER_SIZE" in types
        assert types["MAX_BUFFER_SIZE"].type == TypeDefinitionType.PREPROC_TYPE

        # Preprocessor function defs
        assert "MIN" in types
        assert types["MIN"].type == TypeDefinitionType.PREPROC_FUNCTION

    def test_parse_types_with_name_filter(self, c_challenge_task: ChallengeTask):
        """parse_types_in_code with typename filter should only return matching types."""
        code_ts = CodeTS(c_challenge_task)
        types = code_ts.parse_types_in_code(
            Path("src/sample_project/sample.c"),
            typename="buffer",
        )
        assert "buffer" in types
        assert len(types) == 1

    def test_parse_types_fuzzy_filter(self, c_challenge_task: ChallengeTask):
        """parse_types_in_code with fuzzy filter should match partial names."""
        code_ts = CodeTS(c_challenge_task)
        types = code_ts.parse_types_in_code(
            Path("src/sample_project/sample.c"),
            typename="buffer",
            fuzzy=True,
        )
        # Should match "buffer" struct and "MAX_BUFFER_SIZE" preproc def
        assert len(types) >= 1
        assert "buffer" in types

    def test_forward_declaration_not_matched_as_type(self, c_challenge_task: ChallengeTask):
        """Forward declarations (e.g. `struct forward_only;`) should not produce a type."""
        code_ts = CodeTS(c_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/sample.c"))
        assert "forward_only" not in types

    def test_comment_preceding_function(self, c_challenge_task: ChallengeTask):
        """When a comment precedes a function, the body should include it."""
        code_ts = CodeTS(c_challenge_task)
        func = code_ts.get_function("buffer_init", Path("src/sample_project/sample.c"))
        assert func is not None
        # The comment "Initialize a buffer" should be part of the captured body
        assert "Initialize a buffer" in func.bodies[0].body

    def test_static_function_detected(self, c_challenge_task: ChallengeTask):
        """Static functions should be detected by the parser."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.c"))
        assert "validate_input" in functions
        assert "static" in functions["validate_input"].bodies[0].body

    def test_functions_in_code_with_raw_bytes(self, c_challenge_task: ChallengeTask):
        """get_functions_in_code should work directly with bytes input."""
        code_ts = CodeTS(c_challenge_task)
        code = b"""
int simple_add(int a, int b) {
    return a + b;
}
void noop(void) {}
"""
        functions = code_ts.get_functions_in_code(code, Path("inline.c"))
        assert "simple_add" in functions
        assert "noop" in functions
        assert "return a + b;" in functions["simple_add"].bodies[0].body

    def test_empty_file_returns_no_functions(self, c_challenge_task: ChallengeTask):
        """An empty file should produce no functions."""
        code_ts = CodeTS(c_challenge_task)
        functions = code_ts.get_functions_in_code(b"", Path("empty.c"))
        assert len(functions) == 0

    def test_file_with_only_declarations(self, c_challenge_task: ChallengeTask):
        """A file with only declarations (no bodies) should produce no functions."""
        code_ts = CodeTS(c_challenge_task)
        code = b"""
int foo(int x);
void bar(void);
extern int baz(const char *s);
"""
        functions = code_ts.get_functions_in_code(code, Path("declarations.c"))
        assert len(functions) == 0


# ---------------------------------------------------------------------------
# C++ parsing pipeline integration tests
# ---------------------------------------------------------------------------


class TestCppParsingPipeline:
    """End-to-end tests for C++ code parsing through CodeTS."""

    def test_finds_class_methods(self, cpp_challenge_task: ChallengeTask):
        """Verify class methods are discovered."""
        code_ts = CodeTS(cpp_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.cpp"))

        assert "log" in functions
        assert "getLevel" in functions
        assert "setLevel" in functions
        assert "parse" in functions
        assert "processBuffer" in functions
        assert "getBufferSize" in functions

    def test_finds_free_functions(self, cpp_challenge_task: ChallengeTask):
        """Verify free (non-member) functions are discovered."""
        code_ts = CodeTS(cpp_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.cpp"))

        assert "computeChecksum" in functions
        assert "getVersionString" in functions
        assert "main" in functions

    def test_qualified_function_names(self, cpp_challenge_task: ChallengeTask):
        """Qualified function definitions (Class::method) should be captured."""
        code_ts = CodeTS(cpp_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.cpp"))

        # Parser::log is defined outside the class with qualified name
        log_func = functions.get("log")
        assert log_func is not None
        # Should have the qualified definition's body
        assert any("logger.log(msg)" in b.body for b in log_func.bodies)

    def test_class_types_detected(self, cpp_challenge_task: ChallengeTask):
        """Class definitions should be found in type parsing."""
        code_ts = CodeTS(cpp_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/sample.cpp"))

        assert "Logger" in types
        assert types["Logger"].type == TypeDefinitionType.CLASS

        assert "Parser" in types
        assert types["Parser"].type == TypeDefinitionType.CLASS

    def test_struct_and_enum_types(self, cpp_challenge_task: ChallengeTask):
        """Struct and enum definitions should be found."""
        code_ts = CodeTS(cpp_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/sample.cpp"))

        assert "Config" in types
        assert types["Config"].type == TypeDefinitionType.STRUCT
        assert "max_retries" in types["Config"].definition

        assert "LogLevel" in types
        assert types["LogLevel"].type == TypeDefinitionType.ENUM

    def test_preprocessor_defs_in_cpp(self, cpp_challenge_task: ChallengeTask):
        """Preprocessor definitions should be found in C++ files."""
        code_ts = CodeTS(cpp_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/sample.cpp"))

        assert "VERSION_MAJOR" in types
        assert types["VERSION_MAJOR"].type == TypeDefinitionType.PREPROC_TYPE

        assert "MAKE_VERSION" in types
        assert types["MAKE_VERSION"].type == TypeDefinitionType.PREPROC_FUNCTION

    def test_ifdef_in_cpp(self, cpp_challenge_task: ChallengeTask):
        """Functions in #ifdef blocks should produce multiple bodies in C++."""
        code_ts = CodeTS(cpp_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/sample.cpp"))

        exp_fn = functions.get("experimentalFeature")
        assert exp_fn is not None
        assert len(exp_fn.bodies) == 2

    def test_method_body_content(self, cpp_challenge_task: ChallengeTask):
        """Verify specific method body content."""
        code_ts = CodeTS(cpp_challenge_task)
        func = code_ts.get_function("parse", Path("src/sample_project/sample.cpp"))
        assert func is not None
        assert "memcpy" in func.bodies[0].body
        assert "processBuffer" in func.bodies[0].body


# ---------------------------------------------------------------------------
# Java parsing pipeline integration tests
# ---------------------------------------------------------------------------


class TestJavaParsingPipeline:
    """End-to-end tests for Java code parsing through CodeTS."""

    def test_finds_all_methods(self, java_challenge_task: ChallengeTask):
        """Verify all Java methods are discovered."""
        code_ts = CodeTS(java_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/Sample.java"))

        expected_methods = {
            "validate",
            "getErrorMessage",
            "checkPattern",
            "addItem",
            "processAll",
            "processItem",
            "getItems",
            "main",
        }
        assert expected_methods.issubset(set(functions.keys())), (
            f"Missing: {expected_methods - set(functions.keys())}"
        )

    def test_method_body_content(self, java_challenge_task: ChallengeTask):
        """Verify method bodies contain expected Java source code."""
        code_ts = CodeTS(java_challenge_task)
        functions = code_ts.get_functions(Path("src/sample_project/Sample.java"))

        validate_fn = functions["validate"]
        assert len(validate_fn.bodies) >= 1
        assert "maxLength" in validate_fn.bodies[0].body

    def test_java_type_definitions(self, java_challenge_task: ChallengeTask):
        """Verify extraction of Java types (class, interface, enum)."""
        code_ts = CodeTS(java_challenge_task)
        types = code_ts.parse_types_in_code(Path("src/sample_project/Sample.java"))

        # Interface - maps to CLASS type in the parser
        assert "Validator" in types
        assert types["Validator"].type == TypeDefinitionType.CLASS

        # Classes
        assert "StringValidator" in types
        assert types["StringValidator"].type == TypeDefinitionType.CLASS

        assert "DataProcessor" in types
        assert types["DataProcessor"].type == TypeDefinitionType.CLASS

        # The public class should also be found
        assert "sample_java_project" in types

    def test_get_field_type_name_java(self, java_challenge_task: ChallengeTask):
        """get_field_type_name should resolve field types in Java class definitions."""
        code_ts = CodeTS(java_challenge_task)
        class_def = b"""class DataProcessor {
    private List<String> items;
    private Validator validator;

    public DataProcessor(Validator validator) {
        this.items = new ArrayList<>();
        this.validator = validator;
    }

    public void addItem(String item) {
        if (validator.validate(item)) {
            items.add(item);
        }
    }
}"""
        field_type = code_ts.get_field_type_name(class_def, "validator")
        assert field_type == "Validator"

    def test_get_method_return_type_java(self, java_challenge_task: ChallengeTask):
        """get_method_return_type_name should resolve return types in Java."""
        code_ts = CodeTS(java_challenge_task)
        class_def = b"""class StringValidator implements Validator {
    private int maxLength;

    public boolean validate(String input) {
        return input.length() <= maxLength;
    }

    public String getErrorMessage() {
        return "Error";
    }
}"""
        ret_type = code_ts.get_method_return_type_name(class_def, "validate")
        assert ret_type == "boolean"

        ret_type = code_ts.get_method_return_type_name(class_def, "getErrorMessage")
        assert ret_type == "String"


# ---------------------------------------------------------------------------
# Cross-cutting / edge-case tests
# ---------------------------------------------------------------------------


class TestTreeSitterEdgeCases:
    """Tests for unusual or edge-case inputs to the Tree-sitter pipeline."""

    def test_malformed_c_code_does_not_crash(self, c_challenge_task: ChallengeTask):
        """Parsing severely malformed code should not raise an exception."""
        code_ts = CodeTS(c_challenge_task)
        code = b"""
int broken( {
    // missing closing paren and brace
    return
"""
        # Should not raise -- tree-sitter is error-tolerant
        functions = code_ts.get_functions_in_code(code, Path("broken.c"))
        # We do not assert what is found, just that it does not crash
        assert isinstance(functions, dict)

    def test_very_long_function(self, c_challenge_task: ChallengeTask):
        """A function with many lines should still be parsed correctly."""
        code_ts = CodeTS(c_challenge_task)
        lines = ["int long_func(void) {"]
        for i in range(500):
            lines.append(f"    int x{i} = {i};")
        lines.append("    return x0;")
        lines.append("}")
        code = "\n".join(lines).encode()

        functions = code_ts.get_functions_in_code(code, Path("long.c"))
        assert "long_func" in functions
        body = functions["long_func"].bodies[0]
        assert body.end_line - body.start_line >= 500

    def test_function_with_unicode_in_comments(self, c_challenge_task: ChallengeTask):
        """Unicode in comments should not break parsing."""
        code_ts = CodeTS(c_challenge_task)
        code = """
/* Unicode comment: 日本語テスト, émoji 🎉 */
int unicode_func(void) {
    return 42;
}
""".encode("utf-8")

        functions = code_ts.get_functions_in_code(code, Path("unicode.c"))
        assert "unicode_func" in functions
        assert "return 42;" in functions["unicode_func"].bodies[0].body

    def test_function_caching(self, c_challenge_task: ChallengeTask):
        """Repeated calls should return cached results (lru_cache)."""
        code_ts = CodeTS(c_challenge_task)
        code = b"int cached_fn(void) { return 1; }"
        path = Path("cached.c")

        result1 = code_ts.get_functions_in_code(code, path)
        result2 = code_ts.get_functions_in_code(code, path)

        # The exact same dict object should be returned due to caching
        assert result1 is result2

    def test_multiple_files_independent(self, tmp_path: Path):
        """Parsing multiple files should produce independent results."""
        file_a = "int func_a(void) { return 1; }"
        file_b = "int func_b(void) { return 2; }"

        task = _make_challenge_task(
            tmp_path,
            "c",
            {"a.c": file_a, "b.c": file_b},
        )
        code_ts = CodeTS(task)

        funcs_a = code_ts.get_functions(Path("src/sample_project/a.c"))
        funcs_b = code_ts.get_functions(Path("src/sample_project/b.c"))

        assert "func_a" in funcs_a
        assert "func_b" not in funcs_a
        assert "func_b" in funcs_b
        assert "func_a" not in funcs_b


class TestCodeQueryHelpers:
    """Test helper classes used in the code indexing workflow."""

    def test_cqsearch_result_from_line_valid(self):
        """CQSearchResult.from_line should parse a valid tab-separated line."""
        from buttercup.program_model.codequery import CQSearchResult

        input_line = "my_func\t/path/to/container_src_dir/src/project/file.c:42\tint my_func() {}"
        result = CQSearchResult.from_line(input_line)

        assert result is not None
        assert result.value == "my_func"
        assert result.line == 42
        assert result.body == "int my_func() {}"
        assert "container_src_dir" in str(result.file)

    def test_cqsearch_result_from_line_invalid(self):
        """CQSearchResult.from_line should return None for malformed input."""
        from buttercup.program_model.codequery import CQSearchResult

        # Only one tab -- not enough fields to split into 3
        assert CQSearchResult.from_line("") is None

    def test_cqsearch_result_from_line_no_container_src(self):
        """CQSearchResult.from_line should return None when path lacks container_src_dir."""
        from buttercup.program_model.codequery import CQSearchResult

        input_line = "func\t/some/other/path.c:10\tbody"
        result = CQSearchResult.from_line(input_line)
        assert result is None

    def test_cqsearch_result_from_line_bad_line_number(self):
        """CQSearchResult.from_line should handle non-numeric line numbers gracefully."""
        from buttercup.program_model.codequery import CQSearchResult

        input_line = "func\t/path/container_src_dir/file.c:abc\tbody"
        result = CQSearchResult.from_line(input_line)
        assert result is not None
        assert result.line == 0  # falls back to 0

    def test_function_body_equality(self):
        """FunctionBody equality should compare body text and line numbers."""
        fb1 = FunctionBody("int f() { return 1; }", 10, 12)
        fb2 = FunctionBody("int f() { return 1; }", 10, 12)
        fb3 = FunctionBody("int f() { return 1; }", 10, 13)

        assert fb1 == fb2
        assert fb1 != fb3
        assert hash(fb1) == hash(fb2)

    def test_function_equality_and_hash(self):
        """Function equality should consider name, path, and bodies."""
        body = FunctionBody("int f() { return 1; }", 1, 3)
        f1 = Function("f", Path("a.c"), [body])
        f2 = Function("f", Path("a.c"), [body])
        f3 = Function("g", Path("a.c"), [body])

        assert f1 == f2
        assert f1 != f3
        assert hash(f1) == hash(f2)

    def test_function_has_same_source(self):
        """Function.has_same_source should compare body text ignoring paths."""
        body1 = FunctionBody("int f() { return 1; }", 1, 3)
        body2 = FunctionBody("int f() { return 1; }", 10, 12)

        f1 = Function("f", Path("a.c"), [body1])
        f2 = Function("f", Path("b.c"), [body2])

        assert f1.has_same_source(f2)

    def test_function_has_same_source_different(self):
        """has_same_source should return False for different bodies."""
        body1 = FunctionBody("int f() { return 1; }", 1, 3)
        body2 = FunctionBody("int f() { return 2; }", 1, 3)

        f1 = Function("f", Path("a.c"), [body1])
        f2 = Function("f", Path("a.c"), [body2])

        assert not f1.has_same_source(f2)
