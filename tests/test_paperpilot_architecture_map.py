import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def load_diagram_generator():
    path = ROOT / "docs" / "architecture" / "generate_drawio_diagrams.py"
    spec = importlib.util.spec_from_file_location("paperpilot_diagram_generator", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ArchitectureMapTests(unittest.TestCase):
    def test_architecture_source_contains_current_system_boundaries(self):
        source = (
            ROOT / "docs" / "architecture" / "paperpilot-system-architecture.html"
        ).read_text(encoding="utf-8")

        for label in (
            "PaperPilot Agent Platform",
            "Research Pipeline",
            "Chat Agent Runtime",
            "RAG Retrieval",
            "STORM CORE",
            "Benchmark Registry",
            "SciFact",
            "QASPER",
            "LongMemEval-S",
        ):
            with self.subTest(label=label):
                self.assertIn(label, source)

    def test_readme_embeds_architecture_png_and_source(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            "docs/architecture/paperpilot-executive-overview.svg", readme
        )
        self.assertIn(
            "docs/architecture/paperpilot-executive-overview.drawio", readme
        )
        self.assertNotIn("paperpilot-system-architecture.png", readme)

    def test_drawio_sources_are_editable_and_include_runtime_sequence(self):
        architecture_dir = ROOT / "docs" / "architecture"
        expected = {
            "paperpilot-executive-overview.drawio": (10, 8),
            "paperpilot-agent-system-flow.drawio": (24, 20),
            "paperpilot-async-runtime-sequence.drawio": (9, 14),
        }

        for filename, (minimum_nodes, minimum_edges) in expected.items():
            with self.subTest(filename=filename):
                root = ET.parse(architecture_dir / filename).getroot()
                self.assertEqual(root.tag, "mxfile")
                cells = root.findall(".//mxCell")
                self.assertGreaterEqual(
                    sum(cell.get("vertex") == "1" for cell in cells), minimum_nodes
                )
                self.assertGreaterEqual(
                    sum(cell.get("edge") == "1" for cell in cells), minimum_edges
                )
                vertex_ids = {
                    cell.get("id") for cell in cells if cell.get("vertex") == "1"
                }
                for edge in (cell for cell in cells if cell.get("edge") == "1"):
                    with self.subTest(edge=edge.get("id")):
                        self.assertIn(edge.get("source"), vertex_ids)
                        self.assertIn(edge.get("target"), vertex_ids)

    def test_generator_outputs_match_committed_drawio_and_svg_artifacts(self):
        architecture_dir = ROOT / "docs" / "architecture"
        generator = load_diagram_generator()

        with TemporaryDirectory() as temporary_directory:
            original_root = generator.ROOT
            generator.ROOT = Path(temporary_directory)
            try:
                generator.main()
            finally:
                generator.ROOT = original_root

            for stem in (
                "paperpilot-executive-overview",
                "paperpilot-agent-system-flow",
                "paperpilot-async-runtime-sequence",
            ):
                for suffix in ("drawio", "svg"):
                    filename = f"{stem}.{suffix}"
                    with self.subTest(filename=filename):
                        self.assertEqual(
                            (Path(temporary_directory) / filename).read_bytes(),
                            (architecture_dir / filename).read_bytes(),
                        )

    def test_sequence_edges_reject_missing_or_out_of_range_offsets(self):
        generator = load_diagram_generator()
        with TemporaryDirectory() as temporary_directory:
            original_root = generator.ROOT
            generator.ROOT = Path(temporary_directory)
            try:
                for source_offset, target_offset in ((None, 0.5), (0.5, 1.1)):
                    with self.subTest(
                        source_offset=source_offset, target_offset=target_offset
                    ):
                        diagram = generator.Diagram(
                            "invalid-sequence",
                            400,
                            300,
                            "Invalid sequence",
                            "",
                            [
                                generator.Node("source", 20, 20, 100, 200, "Source"),
                                generator.Node("target", 220, 20, 100, 200, "Target"),
                            ],
                            [
                                generator.Edge(
                                    "source",
                                    "target",
                                    direction="sequence",
                                    source_offset=source_offset,
                                    target_offset=target_offset,
                                )
                            ],
                        )
                        with self.assertRaisesRegex(ValueError, "offset"):
                            generator.write_drawio(diagram)
            finally:
                generator.ROOT = original_root

    def test_async_runtime_sequence_covers_all_participants_and_messages(self):
        sequence = (
            ROOT / "docs" / "architecture" / "paperpilot-async-runtime-sequence.svg"
        ).read_text(encoding="utf-8")

        for participant in (
            "Browser",
            "FastAPI",
            "Async Queue",
            "Agent Runtime",
            "Retriever",
            "LLM",
            "Checkpoint",
            "SSE",
            "Langfuse",
        ):
            with self.subTest(participant=participant):
                self.assertIn(participant, sequence)

        root = ET.parse(
            ROOT / "docs" / "architecture" / "paperpilot-async-runtime-sequence.drawio"
        ).getroot()
        messages = [
            cell
            for cell in root.findall(".//mxCell")
            if cell.get("edge") == "1"
        ]
        self.assertGreaterEqual(len(messages), 14)

    def test_svg_exports_cover_executive_and_agent_flows(self):
        architecture_dir = ROOT / "docs" / "architecture"
        executive = (
            architecture_dir / "paperpilot-executive-overview.svg"
        ).read_text(encoding="utf-8")
        detailed = (
            architecture_dir / "paperpilot-agent-system-flow.svg"
        ).read_text(encoding="utf-8")

        for label in ("业务需求", "智能问答", "深度调研", "业务价值"):
            self.assertIn(label, executive)
        for label in ("Async Queue", "Langfuse", "PIM Domain Pilot"):
            self.assertIn(label, executive)
        for label in (
            "动作路由",
            "统一 Hybrid RAG",
            "arXiv API / Local PDF",
            "StormInformationTable",
            "MiniLM 语义 Top-K",
            "WikiWriter",
            "TopicExpert",
            "问答 / 企业知识库 RAG",
            "Memory 算法",
            "Context 工程",
            "PIM Domain Pilot",
            "Langfuse Score",
            "Async Queue",
        ):
            self.assertIn(label, detailed)
        self.assertNotIn("Retriever Agent", detailed)
        self.assertNotIn("Critic Agent", detailed)

    def test_readme_presents_current_diagrams_and_editable_sources(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        for filename in (
            "paperpilot-executive-overview.svg",
            "paperpilot-executive-overview.drawio",
            "paperpilot-agent-system-flow.svg",
            "paperpilot-async-runtime-sequence.svg",
            "paperpilot-agent-system-flow.drawio",
            "paperpilot-async-runtime-sequence.drawio",
        ):
            with self.subTest(filename=filename):
                self.assertIn(f"docs/architecture/{filename}", readme)


if __name__ == "__main__":
    unittest.main()
