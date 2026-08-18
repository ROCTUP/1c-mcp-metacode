from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from incremental.artifact_sync import (  # noqa: E402
    ArtifactDiff,
    ArtifactSync,
    CodeArtifactCycleContext,
)
from incremental.report import IncrementalReport  # noqa: E402
from incremental import xml_walker  # noqa: E402


PROJECT = "БУХ_ПРОД"
EXT_DIR = "InfostartToolkitCORP"
SOURCE_SCOPE = f"xml_ext:{EXT_DIR}"
CONFIG_NAME = f"{EXT_DIR}$ext$"
CONFIG_QN = f"{PROJECT}/{CONFIG_NAME}"


class FakeState:
    def __init__(
        self,
        *,
        bsl_paths: set[str] | None = None,
        bsl_sidecar_paths: set[str] | None = None,
    ) -> None:
        self.bsl_paths = bsl_paths or set()
        self.bsl_sidecar_paths = bsl_sidecar_paths or set()
        self.deleted_scopes: list[str] = []

    def all_artifact_manifest_rel_paths(self, source_scope: str) -> set[str]:
        return set(self.bsl_paths)

    def all_bsl_file_artifacts(self, source_scope: str) -> list[dict[str, str]]:
        return [{"rel_path": path} for path in self.bsl_sidecar_paths]

    def delete_scope(self, source_scope: str) -> None:
        self.deleted_scopes.append(source_scope)

    def transaction(self):
        return nullcontext()


class FakeSqlite:
    def __init__(self, remaining: int = 0, events: list[str] | None = None) -> None:
        self.remaining = remaining
        self.events = events if events is not None else []

    def count_units_for_config(self, scope: str, config_name: str) -> int:
        self.events.append("check_bsl")
        self.last_args = (scope, config_name)
        return self.remaining


class FakeLoader:
    def __init__(
        self,
        *,
        remaining_graph: int = 0,
        events: list[str] | None = None,
    ) -> None:
        self.remaining_graph = remaining_graph
        self.events = events if events is not None else []

    def delete_extension_scope(self, project_name: str, config_name: str) -> int:
        self.events.append("delete_graph")
        self.delete_args = (project_name, config_name)
        return 27

    def count_extension_scope_nodes(
        self, project_name: str, config_name: str
    ) -> int:
        self.events.append("check_graph")
        return self.remaining_graph


class RemovedExtensionCleanupTests(unittest.TestCase):
    def _context(
        self,
        *,
        sqlite: FakeSqlite | None = None,
    ) -> CodeArtifactCycleContext:
        return CodeArtifactCycleContext(
            project_name=PROJECT,
            base_config_name="БухгалтерияПредприятия",
            base_code_directory=Path("/missing/base"),
            source_mode="xml",
            removed_extension_scopes={SOURCE_SCOPE: CONFIG_NAME},
            known_extension_configs={EXT_DIR: CONFIG_NAME},
            bsl_code_search_scope=PROJECT,
            bsl_code_search_sqlite=sqlite,
        )

    def test_report_marks_removed_extension_as_change_and_merges_it(self) -> None:
        root = IncrementalReport(source_type="xml")
        other = IncrementalReport(source_type="xml")
        other.removed_extension_scopes[SOURCE_SCOPE] = CONFIG_NAME

        root.merge(other)

        self.assertTrue(root.has_changes)
        self.assertEqual(
            {SOURCE_SCOPE: CONFIG_NAME},
            root.removed_extension_scopes,
        )
        self.assertIn("extensions removed=1", root.summary_line())

    def test_xml_top_level_removal_is_deferred_without_deleting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = mock.Mock()
            state.list_extension_scopes.return_value = [SOURCE_SCOPE]
            state.get_extension_scope_config_qn.return_value = CONFIG_QN
            settings = SimpleNamespace(
                load_extensions=True,
                extensions_directory=Path(tmp),
                project_name=PROJECT,
            )
            report = IncrementalReport(source_type="xml")

            with mock.patch.object(
                xml_walker,
                "_detect_base_config_name_xml",
                return_value="БухгалтерияПредприятия",
            ):
                xml_walker.xml_incremental_run_extensions(
                    loader=mock.Mock(),
                    state=state,
                    settings_obj=settings,
                    report=report,
                    base_impact=xml_walker.BaseImpact(),
                )

        self.assertEqual(
            {SOURCE_SCOPE: CONFIG_NAME},
            report.removed_extension_scopes,
        )
        state.delete_scope.assert_not_called()

    def test_removed_extension_bsl_uses_all_manifest_paths_as_deleted(self) -> None:
        paths = {
            f"extensions/{EXT_DIR}/code/CommonModules/A/Ext/Module.bsl",
            f"extensions/{EXT_DIR}/code/CommonModules/B/Ext/Module.bsl",
        }
        sidecar_only_path = (
            f"extensions/{EXT_DIR}/code/CommonModules/C/Ext/Module.bsl"
        )
        state = FakeState(
            bsl_paths=paths,
            bsl_sidecar_paths={sidecar_only_path},
        )
        sync = ArtifactSync.__new__(ArtifactSync)
        sync.state = state
        sync.loader = mock.Mock()
        captured: dict[str, object] = {}

        def capture_apply_bsl(**kwargs):
            captured.update(kwargs)

        sync._apply_bsl = capture_apply_bsl
        context = self._context()

        summaries = sync._prepare_removed_extension_bsl(
            settings_obj=SimpleNamespace(),
            context=context,
            lease=None,
            source_mode="xml",
            source_scope=SOURCE_SCOPE,
            ext_dir_name=EXT_DIR,
            ext_config_name=CONFIG_NAME,
            ext_code_dir=Path("/missing/extensions") / EXT_DIR / "code",
        )

        diff = captured["diff"]
        self.assertIsInstance(diff, ArtifactDiff)
        self.assertEqual(sorted(paths | {sidecar_only_path}), diff.deleted)
        self.assertEqual(CONFIG_NAME, captured["config_name"])
        self.assertEqual(3, next(iter(summaries.values())).deleted)

    def test_finalize_orders_bsl_graph_and_state_cleanup(self) -> None:
        events: list[str] = []
        sqlite = FakeSqlite(events=events)
        loader = FakeLoader(events=events)
        state = FakeState()

        original_delete_scope = state.delete_scope

        def delete_scope(scope: str) -> None:
            events.append("delete_state")
            original_delete_scope(scope)

        state.delete_scope = delete_scope
        sync = ArtifactSync(loader, state)
        context = self._context(sqlite=sqlite)
        report = IncrementalReport(source_type="xml")

        deleted = sync.finalize_removed_extensions(
            settings_obj=SimpleNamespace(enable_bsl_code_search=True),
            context=context,
            report=report,
        )

        self.assertEqual(27, deleted)
        self.assertEqual(
            ["check_bsl", "delete_graph", "check_graph", "delete_state"],
            events,
        )
        self.assertEqual([SOURCE_SCOPE], state.deleted_scopes)
        self.assertNotIn(EXT_DIR, context.known_extension_configs)

    def test_finalize_keeps_state_when_bsl_cleanup_is_incomplete(self) -> None:
        events: list[str] = []
        sqlite = FakeSqlite(remaining=10, events=events)
        loader = FakeLoader(events=events)
        state = FakeState()
        sync = ArtifactSync(loader, state)

        with self.assertRaisesRegex(RuntimeError, "10 units remain"):
            sync.finalize_removed_extensions(
                settings_obj=SimpleNamespace(enable_bsl_code_search=True),
                context=self._context(sqlite=sqlite),
                report=IncrementalReport(source_type="xml"),
            )

        self.assertEqual(["check_bsl"], events)
        self.assertEqual([], state.deleted_scopes)

    def test_finalize_keeps_state_when_graph_postcondition_fails(self) -> None:
        events: list[str] = []
        sqlite = FakeSqlite(events=events)
        loader = FakeLoader(remaining_graph=3, events=events)
        state = FakeState()
        sync = ArtifactSync(loader, state)

        with self.assertRaisesRegex(RuntimeError, "3 nodes remain"):
            sync.finalize_removed_extensions(
                settings_obj=SimpleNamespace(enable_bsl_code_search=True),
                context=self._context(sqlite=sqlite),
                report=IncrementalReport(source_type="xml"),
            )

        self.assertEqual(["check_bsl", "delete_graph", "check_graph"], events)
        self.assertEqual([], state.deleted_scopes)


if __name__ == "__main__":
    unittest.main()
