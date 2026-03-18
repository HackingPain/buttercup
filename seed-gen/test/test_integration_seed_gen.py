"""Integration tests for the seed-gen component.

These tests exercise the seed generation logic end-to-end with realistic inputs,
mocking only external services (Redis, LLM APIs) while testing the actual
processing pipeline.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.messages.tool import ToolCall

# Set required env var before importing modules that need it
if "PYTHON_WASM_BUILD_PATH" not in os.environ:
    os.environ["PYTHON_WASM_BUILD_PATH"] = "/dev/null"

from buttercup.common.challenge_task import ChallengeTask
from buttercup.common.datastructures.msg_pb2 import FunctionCoverage
from buttercup.common.project_yaml import Language
from buttercup.common.task_meta import TaskMeta
from buttercup.program_model.codequery import CodeQueryPersistent
from buttercup.seed_gen.find_harness import HarnessInfo
from buttercup.seed_gen.function_selector import FunctionSelector
from buttercup.seed_gen.task import BaseTaskState, CodeSnippet, Task, TaskName, ToolCallResult
from buttercup.seed_gen.utils import extract_code, get_diff_content


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def realistic_harness_info():
    """Create a realistic harness info for a C fuzzing target."""
    return HarnessInfo(
        file_path=Path("/src/project/fuzz_target.c"),
        code="""#include <stdint.h>
#include <stdlib.h>
#include <string.h>

extern int parse_input(const uint8_t *data, size_t size);

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
    if (size < 4) return 0;

    // Extract header
    uint32_t header = *(uint32_t *)data;
    data += 4;
    size -= 4;

    parse_input(data, size);
    return 0;
}
""",
        harness_name="fuzz_target",
    )


@pytest.fixture
def realistic_java_harness_info():
    """Create a realistic harness info for a Java fuzzing target."""
    return HarnessInfo(
        file_path=Path("/src/project/FuzzTarget.java"),
        code="""import com.code_intelligence.jazzer.api.FuzzedDataProvider;
import com.code_intelligence.jazzer.junit.FuzzTest;

public class FuzzTarget {
    @FuzzTest
    public void fuzzerTestOneInput(FuzzedDataProvider data) {
        String input = data.consumeString(1024);
        int maxLen = data.consumeInt(0, 10000);
        MyParser parser = new MyParser(maxLen);
        parser.parse(input);
    }
}
""",
        harness_name="FuzzTarget",
    )


# ---------------------------------------------------------------------------
# extract_code integration tests
# ---------------------------------------------------------------------------


class TestExtractCodeIntegration:
    """Test code extraction from realistic LLM responses."""

    def test_extract_multiple_seed_functions(self):
        """Should extract a block containing multiple seed generation functions."""
        msg = AIMessage(
            content="""I'll generate seed functions for the PNG parser fuzzer.

```python
import struct
import zlib

def gen_minimal_png() -> bytes:
    # PNG signature
    sig = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    # IHDR chunk
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 0, 0, 0, 0)
    ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xFFFFFFFF)
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
    return sig + ihdr

def gen_large_dimensions() -> bytes:
    sig = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
    ihdr_data = struct.pack('>IIBBBBB', 0xFFFF, 0xFFFF, 16, 2, 0, 0, 0)
    ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xFFFFFFFF)
    ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + ihdr_crc
    return sig + ihdr

def gen_empty_input() -> bytes:
    return b""

def gen_just_signature() -> bytes:
    return bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
```

These functions test various PNG parsing edge cases.""",
        )

        code = extract_code(msg)
        assert "def gen_minimal_png" in code
        assert "def gen_large_dimensions" in code
        assert "def gen_empty_input" in code
        assert "def gen_just_signature" in code
        assert "struct.pack" in code

    def test_extract_code_with_language_specifier(self):
        """Should handle python language specifier in code block."""
        msg = AIMessage(
            content='```python\ndef f() -> bytes:\n    return b"test"\n```',
        )
        code = extract_code(msg)
        assert 'return b"test"' in code

    def test_extract_code_with_no_language_specifier(self):
        """Should handle code blocks without language specifier."""
        msg = AIMessage(
            content='```\ndef f() -> bytes:\n    return b"test"\n```',
        )
        code = extract_code(msg)
        assert 'return b"test"' in code

    def test_extract_code_non_string_content_raises(self):
        """Should raise OutputParserException for non-string content."""
        msg = AIMessage(content=["not", "a", "string"])
        with pytest.raises(OutputParserException):
            extract_code(msg)

    def test_extract_code_non_ai_message_raises(self):
        """Should raise OutputParserException for non-AIMessage input."""
        with pytest.raises(OutputParserException):
            extract_code("not an AIMessage")  # type: ignore[arg-type]

    def test_extract_code_empty_content_raises(self):
        """Should raise OutputParserException for empty content."""
        msg = AIMessage(content="No code blocks here at all")
        with pytest.raises(OutputParserException):
            extract_code(msg)


# ---------------------------------------------------------------------------
# get_diff_content integration tests
# ---------------------------------------------------------------------------


class TestGetDiffContentIntegration:
    """Test diff content extraction with realistic inputs."""

    def test_realistic_diff(self, tmp_path: Path):
        """Should extract content from a realistic unified diff file."""
        diff_content = """--- a/src/parser.c
+++ b/src/parser.c
@@ -42,6 +42,8 @@ int parse_header(const uint8_t *data, size_t len) {
     uint32_t magic = *(uint32_t *)data;
     if (magic != EXPECTED_MAGIC) {
         return -1;
+    } else if (len < MIN_HEADER_SIZE) {
+        return -2;
     }
     return parse_body(data + HEADER_SIZE, len - HEADER_SIZE);
 }
"""
        diff_file = tmp_path / "fix.diff"
        diff_file.write_text(diff_content)

        result = get_diff_content([diff_file])
        assert result is not None
        assert "parse_header" in result
        assert "MIN_HEADER_SIZE" in result

    def test_empty_diff_list(self):
        """Should return None for empty diff list."""
        assert get_diff_content([]) is None

    def test_multiple_diffs_concatenated(self, tmp_path: Path):
        """Should concatenate all diffs when multiple are provided."""
        diff1 = tmp_path / "first.diff"
        diff1.write_text("first diff content")
        diff2 = tmp_path / "second.diff"
        diff2.write_text("second diff content")

        result = get_diff_content([diff1, diff2])
        assert "first diff content" in result
        assert "second diff content" in result


# ---------------------------------------------------------------------------
# FunctionSelector integration tests
# ---------------------------------------------------------------------------


class TestFunctionSelectorIntegration:
    """Test function selection probability calculations with realistic inputs."""

    def test_probabilities_favor_low_coverage(self):
        """Functions with lower coverage should get higher selection probability."""
        coverages = [
            FunctionCoverage(
                function_name="well_tested",
                function_paths=["/src/a.c"],
                covered_lines=90,
                total_lines=100,
            ),
            FunctionCoverage(
                function_name="poorly_tested",
                function_paths=["/src/b.c"],
                covered_lines=10,
                total_lines=100,
            ),
            FunctionCoverage(
                function_name="medium_tested",
                function_paths=["/src/c.c"],
                covered_lines=50,
                total_lines=100,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)

        assert len(funcs) == 3
        assert len(probs) == 3

        # Find index of each function
        names = [f.function_name for f in funcs]
        poorly_idx = names.index("poorly_tested")
        well_idx = names.index("well_tested")

        # Poorly tested function should have higher probability
        assert probs[poorly_idx] > probs[well_idx]

    def test_probabilities_filter_zero_line_functions(self):
        """Functions with zero total lines should be filtered out."""
        coverages = [
            FunctionCoverage(
                function_name="real_func",
                function_paths=["/src/a.c"],
                covered_lines=5,
                total_lines=10,
            ),
            FunctionCoverage(
                function_name="no_lines",
                function_paths=["/src/b.c"],
                covered_lines=0,
                total_lines=0,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)

        assert len(funcs) == 1
        assert funcs[0].function_name == "real_func"

    def test_probabilities_all_zero_lines(self):
        """When all functions have zero lines, should return empty."""
        coverages = [
            FunctionCoverage(function_name="f1", function_paths=[], covered_lines=0, total_lines=0),
            FunctionCoverage(function_name="f2", function_paths=[], covered_lines=0, total_lines=0),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert len(funcs) == 0
        assert len(probs) == 0

    def test_probabilities_empty_input(self):
        """Empty function list should return empty results."""
        funcs, probs = FunctionSelector.calculate_function_probabilities([])
        assert funcs == []
        assert probs == []

    def test_probabilities_sum_to_one(self):
        """Probabilities should sum to approximately 1.0."""
        coverages = [
            FunctionCoverage(
                function_name=f"func_{i}",
                function_paths=[f"/src/f{i}.c"],
                covered_lines=i * 10,
                total_lines=100,
            )
            for i in range(1, 10)
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert abs(sum(probs) - 1.0) < 1e-6

    def test_probabilities_with_temperature(self):
        """Higher temperature should make probabilities more uniform."""
        coverages = [
            FunctionCoverage(
                function_name="low_cov",
                function_paths=["/src/a.c"],
                covered_lines=10,
                total_lines=100,
            ),
            FunctionCoverage(
                function_name="high_cov",
                function_paths=["/src/b.c"],
                covered_lines=90,
                total_lines=100,
            ),
        ]

        _, probs_low_temp = FunctionSelector.calculate_function_probabilities(coverages, temperature=0.1)
        _, probs_high_temp = FunctionSelector.calculate_function_probabilities(coverages, temperature=10.0)

        # With high temperature, difference between probabilities should be smaller
        diff_low_temp = abs(probs_low_temp[0] - probs_low_temp[1])
        diff_high_temp = abs(probs_high_temp[0] - probs_high_temp[1])
        assert diff_high_temp < diff_low_temp

    def test_all_fully_covered_still_returns_functions(self):
        """When all functions have 100% coverage, they should still be selectable."""
        coverages = [
            FunctionCoverage(
                function_name="fully_covered_1",
                function_paths=["/src/a.c"],
                covered_lines=100,
                total_lines=100,
            ),
            FunctionCoverage(
                function_name="fully_covered_2",
                function_paths=["/src/b.c"],
                covered_lines=50,
                total_lines=50,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert len(funcs) == 2
        assert len(probs) == 2
        assert abs(sum(probs) - 1.0) < 1e-6

    def test_single_function(self):
        """A single function should get probability 1.0."""
        coverages = [
            FunctionCoverage(
                function_name="only_func",
                function_paths=["/src/a.c"],
                covered_lines=5,
                total_lines=10,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert len(funcs) == 1
        assert abs(probs[0] - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Task helper method tests
# ---------------------------------------------------------------------------


class TestTaskHelpers:
    """Test Task static/helper methods with realistic inputs."""

    def test_clean_func_name_oss_fuzz_prefix(self):
        """OSS_FUZZ_ prefix should be stripped."""
        assert Task.clean_func_name("OSS_FUZZ_png_sig_cmp") == "png_sig_cmp"
        assert Task.clean_func_name("OSS_FUZZ_main") == "main"

    def test_clean_func_name_file_path_prefix(self):
        """File path prefix (file.c:func) should be stripped."""
        assert Task.clean_func_name("png.c:png_colorspace_check_gamma") == "png_colorspace_check_gamma"
        assert Task.clean_func_name("src/lib.c:helper") == "helper"

    def test_clean_func_name_no_prefix(self):
        """Normal function names should be returned unchanged."""
        assert Task.clean_func_name("normal_function") == "normal_function"
        assert Task.clean_func_name("main") == "main"

    def test_clean_func_name_empty(self):
        """Empty string should be returned unchanged."""
        assert Task.clean_func_name("") == ""

    def test_task_name_enum(self):
        """TaskName enum should have expected values."""
        assert TaskName.SEED_INIT.value == "seed-init"
        assert TaskName.SEED_EXPLORE.value == "seed-explore"
        assert TaskName.VULN_DISCOVERY.value == "vuln-discovery"


# ---------------------------------------------------------------------------
# CodeSnippet and ToolCallResult tests
# ---------------------------------------------------------------------------


class TestDataModels:
    """Test data model serialization with realistic data."""

    def test_code_snippet_str(self):
        """CodeSnippet should produce readable XML-like string."""
        snippet = CodeSnippet(
            file_path=Path("/src/parser.c"),
            code='int parse(const uint8_t *data) { return data[0]; }',
        )
        result = str(snippet)
        assert "<code_snippet>" in result
        assert "/src/parser.c" in result
        assert "parse" in result

    def test_tool_call_result_str(self):
        """ToolCallResult should format tool call and results."""
        snippets = [
            CodeSnippet(file_path=Path("/src/a.c"), code="int a() {}"),
            CodeSnippet(file_path=Path("/src/b.c"), code="int b() {}"),
        ]
        result = ToolCallResult(
            call='get_function_definition("parse")',
            results=snippets,
        )
        result_str = str(result)
        assert "<tool_result>" in result_str
        assert "get_function_definition" in result_str
        assert "/src/a.c" in result_str
        assert "/src/b.c" in result_str

    def test_base_task_state_format_context(self):
        """BaseTaskState.format_retrieved_context should combine multiple results."""
        snippet1 = CodeSnippet(file_path=Path("/src/a.c"), code="int a() {}")
        snippet2 = CodeSnippet(file_path=Path("/src/b.c"), code="int b() {}")

        context = {
            "call1": ToolCallResult(call="get_function_definition(a)", results=[snippet1]),
            "call2": ToolCallResult(call="get_function_definition(b)", results=[snippet2]),
        }

        # We need a minimal Task mock to create BaseTaskState
        mock_task = MagicMock(spec=Task)
        mock_harness = HarnessInfo(
            file_path=Path("/src/fuzz.c"),
            code="int LLVMFuzzerTestOneInput() { return 0; }",
            harness_name="fuzz",
        )

        state = BaseTaskState(
            harness=mock_harness,
            task=mock_task,
            output_dir=Path("/tmp/out"),
            retrieved_context=context,
        )

        formatted = state.format_retrieved_context()
        assert "get_function_definition(a)" in formatted
        assert "get_function_definition(b)" in formatted
        assert "/src/a.c" in formatted
        assert "/src/b.c" in formatted

    def test_base_task_state_empty_context(self):
        """Empty context should produce empty string."""
        mock_task = MagicMock(spec=Task)
        mock_harness = HarnessInfo(
            file_path=Path("/src/fuzz.c"),
            code="int LLVMFuzzerTestOneInput() { return 0; }",
            harness_name="fuzz",
        )

        state = BaseTaskState(
            harness=mock_harness,
            task=mock_task,
            output_dir=Path("/tmp/out"),
        )

        assert state.format_retrieved_context() == ""


# ---------------------------------------------------------------------------
# HarnessInfo tests
# ---------------------------------------------------------------------------


class TestHarnessInfoIntegration:
    """Test HarnessInfo formatting with realistic data."""

    def test_harness_info_str_c(self, realistic_harness_info):
        """C harness info should format as XML-like string."""
        result = str(realistic_harness_info)
        assert "<harness>" in result
        assert "fuzz_target" in result
        assert "LLVMFuzzerTestOneInput" in result
        assert "/src/project/fuzz_target.c" in result

    def test_harness_info_str_java(self, realistic_java_harness_info):
        """Java harness info should format as XML-like string."""
        result = str(realistic_java_harness_info)
        assert "<harness>" in result
        assert "FuzzTarget" in result
        assert "fuzzerTestOneInput" in result
        assert "FuzzedDataProvider" in result


# ---------------------------------------------------------------------------
# Edge case tests for seed generation pipeline inputs
# ---------------------------------------------------------------------------


class TestSeedGenEdgeCases:
    """Test edge cases for seed generation inputs and processing."""

    def test_extract_code_with_nested_backticks(self):
        """Code containing backtick characters should be handled."""
        msg = AIMessage(
            content='```python\ndef f() -> bytes:\n    return b"`test`"\n```',
        )
        code = extract_code(msg)
        assert "return" in code

    def test_extract_code_with_multiple_languages(self):
        """Should extract from the last code block when multiple languages are present."""
        msg = AIMessage(
            content=(
                "Here's the C code:\n```c\nint x = 1;\n```\n"
                "And here's the Python:\n```python\ndef f() -> bytes:\n    return b'test'\n```"
            ),
        )
        code = extract_code(msg)
        assert "def f" in code
        assert "int x" not in code

    def test_code_snippet_with_special_characters(self):
        """CodeSnippet with special characters should work."""
        snippet = CodeSnippet(
            file_path=Path("/src/test.c"),
            code='char *s = "hello\\nworld\\t!";',
        )
        result = str(snippet)
        assert "hello" in result

    def test_diff_content_with_binary_like_content(self, tmp_path: Path):
        """Diff files with unusual content should still be readable."""
        diff_file = tmp_path / "binary.diff"
        diff_file.write_text("--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new with \\x00 bytes\n")

        result = get_diff_content([diff_file])
        assert result is not None
        assert "new with" in result

    def test_function_coverage_with_no_paths(self):
        """FunctionCoverage with empty paths should still work in probability calculation."""
        coverages = [
            FunctionCoverage(
                function_name="orphan_func",
                function_paths=[],
                covered_lines=5,
                total_lines=10,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert len(funcs) == 1
        assert abs(probs[0] - 1.0) < 1e-6

    def test_function_coverage_with_many_paths(self):
        """FunctionCoverage with many paths should work correctly."""
        coverages = [
            FunctionCoverage(
                function_name="multi_path_func",
                function_paths=[f"/src/file{i}.c" for i in range(20)],
                covered_lines=50,
                total_lines=100,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        assert len(funcs) == 1

    def test_probabilities_with_extreme_coverage_ratios(self):
        """Coverage ratios at boundaries (0% and 100%) should work."""
        coverages = [
            FunctionCoverage(
                function_name="zero_cov",
                function_paths=["/src/a.c"],
                covered_lines=0,
                total_lines=100,
            ),
            FunctionCoverage(
                function_name="full_cov",
                function_paths=["/src/b.c"],
                covered_lines=100,
                total_lines=100,
            ),
        ]

        funcs, probs = FunctionSelector.calculate_function_probabilities(coverages)
        # Only the zero coverage function should be in the result
        # since partial functions are preferred over fully covered ones
        assert len(funcs) == 1
        assert funcs[0].function_name == "zero_cov"
