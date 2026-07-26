"""Compile one canonical document into several source-grounded formats."""

from .engine import CompileResult, RepurposerError, compile_document

__all__ = ["CompileResult", "RepurposerError", "compile_document"]
__version__ = "1.0.0"
