"""
IncrementalReport — структура отчёта одного incremental run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from .hashing import ChildDiffStats

if TYPE_CHECKING:
    from .xml_walker import XmlCycleContext


@dataclass(slots=True)
class FormRebuildPair:
    """Per-form pair для form-level extension relationships rebuild.

    Для regular form `ext_form_qn == ext_form_owner_qn`. Для common form
    `ext_form_owner_qn = split(ext_form_qn, '/Form/')[0]` (MetadataObject QN).
    """

    ext_form_qn: str
    base_form_qn: str
    ext_form_owner_qn: str
    base_form_owner_qn: str
    is_common_form: bool


@dataclass(slots=True)
class PostLinkingImpact:
    """Per-config impact для Phase 4 PostLinkingSync.

    Единственный source of truth для handler refresh targets и form-level
    extension rebuild scope в incremental cycle. Заполняется в metadata и
    artifact phases; читается в `PostLinkingSync.run`.
    """

    forms_by_config: Dict[str, Set[str]] = field(default_factory=dict)
    commands_by_config: Dict[str, Set[str]] = field(default_factory=dict)
    url_methods_by_config: Dict[str, Set[str]] = field(default_factory=dict)
    event_subscriptions_by_config: Dict[str, Set[str]] = field(default_factory=dict)
    configs_for_handler_relink: Set[str] = field(default_factory=set)

    # Per-form scope для form-level extension relationships rebuild.
    base_forms_with_changed_internals: Set[str] = field(default_factory=set)
    ext_forms_with_changed_internals: Dict[str, Set[str]] = field(default_factory=dict)

    def _bucket_add(self, bucket: Dict[str, Set[str]], config_name: str, qn: str) -> None:
        if not config_name or not qn:
            return
        bucket.setdefault(config_name, set()).add(qn)

    def add_form(self, config_name: str, form_qn: str) -> None:
        self._bucket_add(self.forms_by_config, config_name, form_qn)

    def add_command(self, config_name: str, command_qn: str) -> None:
        self._bucket_add(self.commands_by_config, config_name, command_qn)

    def add_url_method(self, config_name: str, url_method_qn: str) -> None:
        self._bucket_add(self.url_methods_by_config, config_name, url_method_qn)

    def add_event_subscription(self, config_name: str, qn: str) -> None:
        self._bucket_add(self.event_subscriptions_by_config, config_name, qn)

    def mark_handler_relink(self, config_name: str) -> None:
        if config_name:
            self.configs_for_handler_relink.add(config_name)

    def mark_base_form_internals_changed(self, base_form_qn: str) -> None:
        if base_form_qn:
            self.base_forms_with_changed_internals.add(base_form_qn)

    def mark_ext_form_internals_changed(self, ext_config_name: str, ext_form_qn: str) -> None:
        self._bucket_add(self.ext_forms_with_changed_internals, ext_config_name, ext_form_qn)

    def is_empty(self) -> bool:
        return not (
            self.forms_by_config
            or self.commands_by_config
            or self.url_methods_by_config
            or self.event_subscriptions_by_config
            or self.configs_for_handler_relink
            or self.base_forms_with_changed_internals
            or self.ext_forms_with_changed_internals
        )

    @staticmethod
    def _merge_bucket(
        dst: Dict[str, Set[str]], src: Dict[str, Set[str]]
    ) -> None:
        for cfg, qns in src.items():
            if qns:
                dst.setdefault(cfg, set()).update(qns)

    def merge(self, other: "PostLinkingImpact") -> None:
        self._merge_bucket(self.forms_by_config, other.forms_by_config)
        self._merge_bucket(self.commands_by_config, other.commands_by_config)
        self._merge_bucket(self.url_methods_by_config, other.url_methods_by_config)
        self._merge_bucket(
            self.event_subscriptions_by_config, other.event_subscriptions_by_config
        )
        if other.configs_for_handler_relink:
            self.configs_for_handler_relink.update(other.configs_for_handler_relink)
        if other.base_forms_with_changed_internals:
            self.base_forms_with_changed_internals.update(
                other.base_forms_with_changed_internals
            )
        self._merge_bucket(
            self.ext_forms_with_changed_internals, other.ext_forms_with_changed_internals
        )


@dataclass(slots=True)
class AdoptedFromImpact:
    """Накопитель точечных целей для scoped ADOPTED_FROM refresh.

    `exact_qns_by_label` — exact QN-узлов по их labels (Attribute, Resource, ...).
    `prefix_qns` — плоский label-agnostic set: для каждого prefix builder
    проходит ВСЕ 15 metadata-level labels с условием
    `(qualified_name = p OR qualified_name STARTS WITH p + '/')`. Это покрывает
    И parent-узел prefix-а, И все его metadata-level descendants за один проход.
    Используется для:
    - changed TabularPart / changed UrlTemplate (parent + nested children);
    - added MetadataObject (весь subtree adopted объекта);
    - changed object с changed `ПринадлежностьОбъекта` (subtree per-node filter
      сделает builder);
    - XML BaseImpact projection (added∪changed base target → subtree extension
      descriptor-а получает refresh ADOPTED_FROM).

    `full_refresh_required=True` — fallback на полный `_refresh_extension_links`
    (baseline scope пуст, rename, аварийная ошибка scoped builder).
    """

    exact_qns_by_label: Dict[str, Set[str]] = field(default_factory=dict)
    prefix_qns: Set[str] = field(default_factory=set)
    full_refresh_required: bool = False

    def add_exact(self, label: str, qn: str) -> None:
        self.exact_qns_by_label.setdefault(label, set()).add(qn)

    def add_prefix(self, qn: str) -> None:
        self.prefix_qns.add(qn)

    def is_empty(self) -> bool:
        if self.full_refresh_required:
            return False
        if self.prefix_qns:
            return False
        return not any(self.exact_qns_by_label.values())

    def merge(self, other: "AdoptedFromImpact") -> None:
        if other.full_refresh_required:
            self.full_refresh_required = True
        for label, qns in other.exact_qns_by_label.items():
            if qns:
                self.exact_qns_by_label.setdefault(label, set()).update(qns)
        if other.prefix_qns:
            self.prefix_qns.update(other.prefix_qns)


@dataclass(slots=True)
class ExtensionRemovalReport:
    """Lifecycle-событие удаления extension scope за один цикл.

    Отдельное поле, а не подмешивание Configuration QN в `deleted_qns`:
    последнее семантически про `MetadataObject`.
    """

    source_scope: str
    ext_dir_name: str
    config_qn: str
    reason: str
    neo4j_nodes_deleted: int = 0
    # Durable-маркер из очереди, а не счётчик этого цикла: именно по нему
    # решается, обновлять ли кэш статистики. Retry после падения между батчами
    # физически удалит 0 узлов, но кэш всё равно устарел.
    graph_refresh_required: bool = False
    bsl_routines_purged: int = 0
    bsl_outcome: str = ""
    summaries_quarantined: bool = False
    finalized: bool = False
    superseded: bool = False
    resumed: bool = False
    attempts: int = 0
    error: Optional[str] = None
    # Маршрут BSL-стадии. Главное, что должно быть видно одной строкой отчёта:
    # обошлись ли scoped-вычитанием или заплатили полной перестройкой корпуса
    # (и по какой именно причине).
    bsl_mode: str = ""
    bsl_routines_prepared: int = 0
    bsl_routines_applied: int = 0
    bsl_corpus_docs_decremented: int = 0
    bsl_full_phase_a_required: bool = False
    bsl_fallback_reason: str = ""

    def summary(self) -> str:
        if self.superseded:
            return (
                f"ext_cleanup superseded: scope={self.source_scope} "
                f"config={self.config_qn} reason={self.reason}"
            )
        parts = [
            f"scope={self.source_scope}",
            f"config={self.config_qn}",
            f"reason={self.reason}",
            f"nodes_deleted={self.neo4j_nodes_deleted}",
            f"bsl={self.bsl_outcome or 'n/a'}",
            f"bsl_routines={self.bsl_routines_purged}",
            f"summaries_quarantined={self.summaries_quarantined}",
            f"finalized={self.finalized}",
        ]
        if self.bsl_mode:
            parts.append(f"bsl_mode={self.bsl_mode}")
        if self.bsl_routines_prepared:
            parts.append(f"bsl_prepared={self.bsl_routines_prepared}")
        if self.bsl_routines_applied:
            parts.append(f"bsl_applied={self.bsl_routines_applied}")
        if self.bsl_corpus_docs_decremented:
            parts.append(f"bsl_docs_decremented={self.bsl_corpus_docs_decremented}")
        parts.append(f"full_phase_a={self.bsl_full_phase_a_required}")
        if self.bsl_fallback_reason:
            parts.append(f"fallback_reason={self.bsl_fallback_reason}")
        if self.resumed:
            parts.append("resumed=True")
        if self.attempts:
            parts.append(f"attempts={self.attempts}")
        if self.error:
            parts.append(f"error={self.error}")
        return "ext_cleanup: " + " ".join(parts)


@dataclass(slots=True)
class IncrementalReport:
    """Per-run report incremental loading.

    Поля заполняются `MetadataIncrementalSync` и используются scheduler-ом для:
    - Логирования (added/changed/deleted counts на object- и child-уровнях).
    - Триггера post-sync metadata embedding re-pass (`embedding_repass_needed_qns`).
    - Сбора `BaseImpact` для extension overlay refresh (`configuration_changed`).

    `extension_reports` — per-scope под-отчёты для каждого `<ext_dir>`. Базовый отчёт
    остаётся в основных полях (added_qns/changed_qns/...), расширения агрегируются
    отдельно для раздельного логирования.
    """

    source_type: str = ""

    added_qns: List[str] = field(default_factory=list)
    changed_qns: List[str] = field(default_factory=list)
    deleted_qns: List[str] = field(default_factory=list)
    unchanged_count: int = 0

    # changed objects, у которых description_embedding был invalidated
    # (embedding re-pass нужен; added всегда требуют embedding).
    changed_qns_with_invalidated_embedding: List[str] = field(default_factory=list)

    # Child-level diff aggregated across changed objects.
    # Печатаются в summary_line только ненулевые группы.
    child_stats: ChildDiffStats = field(default_factory=ChildDiffStats)

    # True если base Configuration hash изменился в текущем цикле.
    # Используется scheduler-ом для построения BaseImpact в XML extension overlay refresh.
    configuration_changed: bool = False

    # True если в этом цикле изменился подсистемный owner-set для SSL API
    # (любой added/changed/deleted объект category=Подсистемы или modification
    # `Состав`/`ПутьПодсистемы` поля). Scheduler триггерит
    # loader.refresh_ssl_api_for_project в Phase 4.5 при этом флаге.
    ssl_owners_dirty: bool = False

    # Накопитель точечных impact-целей для scoped ADOPTED_FROM refresh.
    # Заполняется в diff_and_apply_configuration / apply_added_object /
    # apply_changed_object / XML BaseImpact projection. Используется
    # `_refresh_extension_links_scoped` для решения full vs scoped vs skip.
    adopted_from_impact: AdoptedFromImpact = field(default_factory=AdoptedFromImpact)

    # Per-config impact для Phase 4 PostLinkingSync. Заполняется metadata phase
    # (child diff: commands/url_methods/forms/event_subscriptions) и artifact
    # phase (Form.xml workers, BSL routine delta). Sub-report для extension
    # имеет свой `post_linking_impact`; scheduler собирает aggregated impact в
    # `CodeArtifactCycleContext` через merge root + extension_reports[*].
    post_linking_impact: "PostLinkingImpact" = field(default_factory=lambda: PostLinkingImpact())

    # Per-extension sub-reports: key = ext_dir_name, value = IncrementalReport scope-а.
    extension_reports: Dict[str, "IncrementalReport"] = field(default_factory=dict)

    # Lifecycle удалённых extension scope за этот цикл (включая продолженные
    # после рестарта) и число записей очереди, оставшихся незавершёнными.
    removed_extension_scopes: List["ExtensionRemovalReport"] = field(default_factory=list)
    pending_extension_cleanups: int = 0

    duration_seconds: float = 0.0
    errors: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    # Per-cycle XML context (один scan базы + lazy ext scans + scoped overlay
    # provider). Заполняется в `MetadataIncrementalSync._sync_xml_impl` и
    # читается scheduler-ом для прокидывания в `_run_xml_extensions_phase`.
    # Не сериализуется и не персистится — живёт ровно tick.
    xml_context: Optional[Any] = None

    @property
    def embedding_repass_needed_qns(self) -> Set[str]:
        result = set(self.added_qns) | set(self.changed_qns_with_invalidated_embedding)
        for sub in self.extension_reports.values():
            result |= set(sub.added_qns) | set(sub.changed_qns_with_invalidated_embedding)
        return result

    @property
    def has_changes(self) -> bool:
        if self.added_qns or self.changed_qns or self.deleted_qns:
            return True
        return any(sub.has_changes for sub in self.extension_reports.values())

    @property
    def has_graph_changes(self) -> bool:
        """True если metadata-слой действительно изменил Neo4j-граф: added/changed/deleted
        (рекурсивно по расширениям через has_changes), configuration_changed, непустой
        post_linking_impact на корне либо в любом extension sub-report, или удаление
        extension scope.

        Для удалений смотрим на durable `graph_refresh_required` из очереди, а не
        на `neo4j_nodes_deleted` этого цикла: иначе финализация после рестарта
        (узлы снёс упавший предыдущий цикл, повтор удалил 0) не обновила бы кэш
        статистики, хотя counted counts уже устарели. Superseded-записи ничего
        не удаляли и на решение не влияют."""
        if self.has_changes:
            return True
        if self.configuration_changed or not self.post_linking_impact.is_empty():
            return True
        if any(
            r.graph_refresh_required and not r.superseded
            for r in self.removed_extension_scopes
        ):
            return True
        return any(
            sub.configuration_changed or not sub.post_linking_impact.is_empty()
            for sub in self.extension_reports.values()
        )

    def merge(self, other: "IncrementalReport") -> None:
        """Объединить два под-report (incremental + full XML scan)."""
        self.added_qns.extend(other.added_qns)
        self.changed_qns.extend(other.changed_qns)
        self.deleted_qns.extend(other.deleted_qns)
        self.unchanged_count += other.unchanged_count
        self.changed_qns_with_invalidated_embedding.extend(
            other.changed_qns_with_invalidated_embedding
        )
        self.child_stats.merge(other.child_stats)
        if other.configuration_changed:
            self.configuration_changed = True
        self.adopted_from_impact.merge(other.adopted_from_impact)
        self.post_linking_impact.merge(other.post_linking_impact)
        self.removed_extension_scopes.extend(other.removed_extension_scopes)
        self.pending_extension_cleanups += other.pending_extension_cleanups
        for ext_name, sub in other.extension_reports.items():
            existing = self.extension_reports.get(ext_name)
            if existing is None:
                self.extension_reports[ext_name] = sub
            else:
                existing.merge(sub)
        self.duration_seconds += other.duration_seconds
        self.errors.extend(other.errors)
        self.notes.extend(other.notes)

    def summary_line(self) -> str:
        parts: List[str] = [
            f"[{self.source_type or 'unknown'}]",
            (
                f"objects: added={len(self.added_qns)} changed={len(self.changed_qns)} "
                f"deleted={len(self.deleted_qns)} unchanged={self.unchanged_count}"
            ),
        ]
        for group, bucket in self.child_stats.nonzero_groups():
            parts.append(
                f"{group}: added={bucket['added']} "
                f"changed={bucket['changed']} deleted={bucket['deleted']}"
            )
        if self.extension_reports:
            ext_changed = sum(1 for s in self.extension_reports.values() if s.has_changes)
            parts.append(
                f"extensions scanned={len(self.extension_reports)} changed={ext_changed}"
            )
        parts.append(
            f"errors={len(self.errors)} duration={self.duration_seconds:.2f}s"
        )
        return "; ".join(parts)

    def detailed_summary_lines(self) -> List[str]:
        """Базовая строка + по строке на каждое изменённое расширение и на
        каждое lifecycle-событие удаления scope."""
        lines: List[str] = [self.summary_line()]
        for ext_name in sorted(self.extension_reports.keys()):
            sub = self.extension_reports[ext_name]
            if sub.has_changes:
                lines.append(f"  ext={ext_name}: {sub.summary_line()}")
        for removal in self.removed_extension_scopes:
            lines.append(f"  {removal.summary()}")
        if self.pending_extension_cleanups:
            lines.append(
                f"  ext_cleanup pending: {self.pending_extension_cleanups} scope(s) "
                "will be retried next cycle"
            )
        return lines
