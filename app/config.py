"""
Configuration module for 1C Metadata to Neo4j Loader and MCP Server
"""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from pathlib import Path
from typing import Optional, List, Dict, Any


APP_VERSION = "2.0.1"


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    # Neo4j Database Configuration
    neo4j_uri: str = "bolt://1c-neo4j:7687"
    neo4j_username: str = "neo4j"
    # Do not store secrets in repository. Provide via environment: NEO4J_PASSWORD
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"

    # Embedding API Configuration
    embedding_api_base: Optional[str] = "http://host.docker.internal:1234/v1"
    embedding_api_key: Optional[str] = "lm-api-key"
    embedding_proxy: Optional[str] = None
    embedding_model: Optional[str] = "text-embedding-qwen3-embedding-0.6b"
    # Timeouts and retries for embedding requests
    embedding_timeout_seconds: float = 1800.0
    embedding_max_retries: int = 3
    # Backoff/jitter tuning for retries (optional)
    embedding_retry_backoff_base_seconds: float = 0.5
    embedding_retry_backoff_max_seconds: float = 4.0
    embedding_retry_jitter_seconds: float = 0.2
    # При "No embedding data received" повторить тот же OpenAI-compatible batch с encoding_format=float
    # (fallback внутри той же retry-попытки, не отдельный retry). Отключается значением false.
    embedding_float_fallback_on_no_data: bool = True
    # Optional distinct timeout for model info retrieval (falls back to embedding_timeout_seconds if None)
    embedding_model_info_timeout_seconds: Optional[float] = None
    # Short bounded timeout for the startup vector-index dimension probe (restart repair path).
    # Kept small so an unavailable/slow embedding endpoint cannot stall post-bootstrap startup.
    embedding_startup_probe_timeout_seconds: float = 10.0
    # Project fingerprint cosine similarity threshold to avoid unnecessary reindexing
    embedding_fingerprint_cosine_threshold: float = 0.999

    # Embedding input formatting / transport routing.
    # See app/graphdb/embedding_text_format.py for profile/transport tables.
    embedding_text_format_profile: str = "auto"
    embedding_transport: str = "auto"
    embedding_description_query_instruction: str = "Given a search query, retrieve relevant passages that answer the query"

    # Enable embeddings for specific entities
    enable_routine_description_embedding: bool = False
    enable_metadata_description_embedding: bool = False

    # Embedding indexing configuration
    embedding_indexing_workers: int = 6           # Number of parallel workers for indexing
    embedding_batch_size: int = 100               # Batch size for API requests
    embedding_save_batch_size: int = 50           # Batch size for saving to Neo4j
    embedding_rate_limit_delay: float = 0.05      # Delay between batches (seconds) for rate limiting

    # Description embedding outer-round retry: total number of phase passes in a
    # single run (NOT "1 + retries"), shared by routine and metadata description
    # phases. 3 = the main pass plus up to two extra passes over the still-remaining
    # `..._embedding IS NULL` nodes. <= 0 is clamped to 1 so the setting never
    # disables indexing. Retry ownership (three distinct layers):
    #   - the `encoding_format="float"` retry inside a single embedding batch;
    #   - EMBEDDING_MAX_RETRIES fixes a single embedding HTTP request in a batch;
    #   - these rounds finish the remaining descriptions within the current run
    #     when a worker classified the endpoint as unavailable mid-pass.
    # Backoff applies ONLY between rounds and ONLY after a round ended in an
    # embedding outage; it does not replace EMBEDDING_MAX_RETRIES.
    embedding_description_indexing_max_rounds: int = 3
    embedding_description_indexing_round_backoff_seconds: float = 20.0
    embedding_description_indexing_round_backoff_jitter_seconds: float = 5.0

    # Embedding chunking/pooling configuration
    # - Detect max input tokens via API; fallback to explicit value when not available
    embedding_detect_context_via_api: bool = True
    embedding_max_input_tokens_fallback: int = 4000
    # Safety margin and token->char fallback for environments without local tokenizer
    embedding_chunk_safety_ratio: float = 0.9
    embedding_chars_per_token_fallback: float = 2.0
    # Chunking behavior for long descriptions (indexing only; queries remain unchunked)
    embedding_chunk_overlap_chars: int = 200
    embedding_max_chunks_per_object: int = 12
    # Pooling and normalization
    # Chunk-level pooling is currently fixed in code: weighted_mean_pool(...).
    # Future option placeholder, intentionally inactive:
    # embedding_chunk_pooling: str = "weighted_mean"
    embedding_l2_norm_chunks: bool = True
    embedding_l2_norm_final: bool = True

    # File Paths - Hardcoded; cannot be overridden via environment variables.
    # Base data directory inside the container (docker-compose mounts ./data -> /app/data)
    @property
    def data_directory(self) -> Path:
        return Path("/app/data")

    # Derived directories under data_directory
    @property
    def metadata_directory(self) -> Path:
        return self.data_directory / "metadata"

    # 1C configuration code dump directory (for Predefined.xml search)
    @property
    def code_directory(self) -> Path:
        return self.data_directory / "code"

    # 1C extensions directory (for loading multiple extensions)
    @property
    def extensions_directory(self) -> Path:
        return self.data_directory / "extensions"

    @property
    def app_config_directory(self) -> Path:
        return Path("config")

    @property
    def console_agent_llm_config_path(self) -> Path:
        return self.app_config_directory / "console_agent_llm.yaml"

    # MCP Server Configuration
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 6001
    mcp_path: str = "/mcp"
    mcp_settings_sqlite_path: str = "storage/mcp/settings.sqlite"

    # Web Console
    web_console_enabled: bool = True
    web_console_admin_token: str = ""
    web_console_users_sqlite_path: str = "storage/console/users.sqlite"
    web_console_seed_users: List[Dict[str, Any]] = Field(default_factory=list)
    web_console_path: str = "/console"
    web_console_api_prefix: str = "/api/console"

    # Web Console Agent
    console_agent_enabled: bool = False
    console_agent_model: str = "deepseek/deepseek-v4-flash"
    console_agent_llm_api_base: Optional[str] = "https://openrouter.ai/api/v1"
    console_agent_llm_api_key: Optional[str] = None
    console_agent_llm_proxy: Optional[str] = None
    console_agent_llm_timeout: float = 180.0
    console_agent_llm_temperature: float = 0.3
    console_agent_reasoning_effort: Optional[str] = "medium"
    console_agent_reasoning_summary: Optional[str] = "auto"
    console_agent_show_reasoning: bool = True
    console_agent_max_turns: int = 100
    console_agent_session_sqlite_path: str = "storage/console_agent/sessions.sqlite"
    console_agent_chats_sqlite_path: str = "storage/console_agent/chats.sqlite"
    console_agent_max_chats_per_user: int = 100
    console_agent_mcp_timeout: float = 180.0
    console_agent_local_mcp_excluded_tools: List[str] = Field(default_factory=list)
    console_agent_external_mcp_servers: List[Dict[str, Any]] = Field(default_factory=list)
    console_agent_session_item_limit: Optional[int] = 250
    console_agent_tool_output_trimmer_enabled: bool = True
    console_agent_tool_output_recent_turns: int = 5
    console_agent_tool_output_max_chars: int = 20000
    console_agent_tool_output_preview_chars: int = 20000

    # Project Settings
    project_name: str = "1C Project"
    related_projects: List[str] = Field(default_factory=list)

    @property
    def allowed_projects(self) -> List[str]:
        return [self.project_name] + self.related_projects
    
    # Debug Configuration
    enable_debug: bool = False
    debug_log_max_data_length: int = 100  # Maximum characters to show for database parameters in debug logs

    # Runtime memory management
    # При true после тяжёлых фаз (full metadata load, BSL Phase A/B finalize)
    # вызывается gc.collect()+malloc_trim(0) на границах фаз (не в hot loops),
    # чтобы вернуть ОС allocator-retained (glibc arena) память. Env: MEMORY_TRIM_ENABLED.
    # false — отключить эти паузы (например, для сравнения производительности).
    memory_trim_enabled: bool = True

    # Loader control
    # FULL_METADATA_RELOAD: set to "true"/"1" to force full reload (clear DB) on startup
    full_metadata_reload: bool = False

    # ====================
    # Incremental loading (stage 1: metadata TXT/XML)
    # ====================
    # Глобальный флаг: включает scheduler + one-shot incremental check после старта MCP.
    # При false поведение проекта не меняется (только FULL_METADATA_RELOAD / bootstrap empty).
    incremental_loading_enabled: bool = True
    # Включает периодический re-check по интервалу. При false — только one-shot first cycle.
    incremental_loading_schedule_enabled: bool = True
    # Интервал между циклами scheduler-а (только если schedule_enabled=true).
    incremental_loading_interval_minutes: int = 60
    # Путь к SQLite state. Симметрично BSL_CODE_SEARCH_SQLITE_PATH:
    # path используется как есть (Path(...)). В docker CWD=/app → 'storage/...' = '/app/storage/...'
    # → попадает на named volume (storage_prj*), не на bind-mounted /app/data.
    incremental_loading_state_path: str = "storage/incremental/incremental_loading.sqlite"
    # Overlap для XML watermark (защита от skew часов на диске).
    incremental_loading_overlap_seconds: int = 60

    # Full reconcile — детектирует удалённые artifacts/XML descriptors (которые обычный run пропускает).
    # Применяется и к metadata XML, и к code artifacts (Form.xml/.bsl/Predefined/Help/...), и к
    # base, и к extensions. Для XML это не означает второй обход дерева — переиспользуется
    # `xml_context.code_index` текущего цикла.
    incremental_full_reconcile_enabled: bool = True
    incremental_full_reconcile_interval_hours: int = 24
    incremental_full_reconcile_window_start: str = "02:00"
    incremental_full_reconcile_window_end: str = "05:00"

    # При обычном рестарте startup incremental перехватывает свежий scheduler_lock
    # без ожидания stale_after_seconds. Scheduled scheduler по-прежнему уважает lock.
    # Default true рассчитан на single-container deployment.
    incremental_startup_lock_takeover: bool = True

    # Toggle loading of predefined values (Predefined.xml). Env: LOAD_PREDEFINED_VALUES
    # Default = True (load). Set to false to skip parsing/loading Predefined.xml.
    load_predefined_values: bool = True

    # Toggle loading of Managed Form XCF (Ext/Form.xml) definitions. Env: LOAD_FORMS_FROM_XML
    # Default = True (load). Set to false to skip parsing/loading Form.xml.
    load_forms_from_xml: bool = True
    # Toggle loading of BSL signatures from .bsl files. Env: LOAD_BSL_SIGNATURES
    # Default = True (load). Set to false to skip parsing/loading .bsl modules and routines.
    load_bsl_signatures: bool = True
    # Toggle loading of Help content from Help/ru.html for metadata objects. Env: LOAD_HELP_FROM_HTML
    # Default = True (load). Set to false to skip parsing/loading Help/ru.html.
    load_help_from_html: bool = True

    # Toggle loading of Role rights from Roles/*/Ext/Rights.xml. Env: LOAD_ROLE_RIGHTS
    # Default = True (load). Set to false to skip rights import.
    load_role_rights: bool = True

    # Toggle loading of Event Subscriptions from EventSubscriptions/*.xml. Env: LOAD_EVENT_SUBSCRIPTIONS
    # Default = True (load). Set to false to skip loading Event Subscriptions.
    load_event_subscriptions: bool = True

    # Toggle loading of 1C extensions from extensions directory. Env: LOAD_EXTENSIONS
    # Default = True (load). Set to false to skip loading extensions.
    load_extensions: bool = True

    # Toggle enriching nodes with GUIDs from ConfigDumpInfo.xml. Env: LOAD_METADATA_GUIDS
    # Default = True (enabled). Set to false to skip reading ConfigDumpInfo.xml and meta_uuid enrichment.
    load_metadata_guids: bool = True

    # Metadata source: 'txt' parses metadata/*.txt; 'xml' parses code/<...>.xml directly.
    # Env: METADATA_SOURCE
    metadata_source: str = "xml"

    # XML standard attributes mode (XML source only; ignored for txt). Env: XML_STANDARD_ATTRIBUTES_MODE
    # - 'listed' (default): keep/enrich only standard attrs explicitly listed in XML; do not
    #   materialize absent ones. Safe mode that avoids broad-Number noise.
    # - 'materialized': additionally materialize absent standard attrs (diagnostic/full mode).
    xml_standard_attributes_mode: str = "listed"

    # Audit Logging
    enable_log: bool = False

    # Query and Batching
    # Maximum rows to return from typed-tool queries (MCP).
    query_max_results: int = 1000
    # Batch size for UNWIND-based bulk loads
    neo4j_batch_size: int = 1000
    # Maximum batch size in MB for BSL routines with body (adaptive batching)
    neo4j_bsl_batch_max_mb: float = 3.0
    # Reopen session every N chunks to prevent memory buildup (0 = never reopen)
    neo4j_session_refresh_interval: int = 100
    # Batch size for project cleanup (FULL_METADATA_RELOAD) to avoid OOM (4GB transaction limit)
    # Env: NEO4J_CLEAR_PROJECT_BATCH_SIZE
    neo4j_clear_project_batch_size: int = 10000

    # ====================
    # Parallel Processing Settings
    # ====================
    # Control multiprocessing and threading for file parsing.
    # Adjust based on CPU cores and workload characteristics:
    # - BSL files: CPU-bound, benefit from multiprocessing
    # - XML files: I/O-bound, may benefit more from threading
    # - Form.bin: Mixed workload, test to find optimal settings

    PROCESS_WORKERS: int = 6       # Number of worker processes for parallel parsing
    THREADS_PER_PROCESS: int = 4    # Threads per process (1-8, increase for I/O-bound tasks)

    # Per-file-type overrides (optional, use PROCESS_WORKERS/THREADS_PER_PROCESS if not set)
    BSL_PROCESS_WORKERS: Optional[int] = None      # Override for .bsl files
    BSL_THREADS_PER_PROCESS: Optional[int] = None  # Override for .bsl files
    XML_PROCESS_WORKERS: Optional[int] = None      # Override for .xml files (future)
    XML_THREADS_PER_PROCESS: Optional[int] = None  # Override for .xml files (future)

    # Incremental artifact hashing / parse-only worker pool.
    # Used by hash_files_parallel() и parse_bsl_files_parallel(): один общий
    # ProcessPoolExecutor для baseline init и phase 2/3 diff'а.
    INCREMENTAL_ARTIFACT_WORKERS: Optional[int] = None  # default: BSL_PROCESS_WORKERS or PROCESS_WORKERS or 4

    # Optional detailed parallel logging (process/thread IDs, worker lifecycle, batch commits)
    # Enable to verify true parallelism; keep False in production to reduce log noise.
    enable_parallel_logging: bool = False

    # Optional hybrid search logging (search parameters, result counts)
    # Enable to debug hybrid search behavior; keep False in production to reduce log noise.
    enable_hybrid_logging: bool = False

    # Neo4j driver timeouts and pool (configurable via environment)
    neo4j_connection_timeout: float = 180.0   # seconds; handshake/connect deadline
    neo4j_max_conn_lifetime: float = 3600.0   # seconds; lifetime in pool
    neo4j_pool_size: int = 50                 # max connections in pool
    neo4j_max_tx_retry_time: float = 120.0    # seconds; total retry budget for transient errors
    neo4j_fetch_size: int = 1000              # rows per pull for read queries

    # Pool acquisition timeout (seconds) for getting a connection from the pool
    neo4j_pool_acq_timeout: float = 300.0

    # Optional staggered startup to reduce concurrent handshake/DDL storms
    startup_delay_seconds: int = 0

    query_default_limit: int = 100
    # Response formatting
    response_format: str = "toon"  # 'json', 'text', or 'toon'
    response_compact_nodes: bool = True
    response_json_compact: bool = True
    response_compact_refs: bool = True  # Replace repeated *_qn/*_config_name with @qn:N/@config:N refs in json output

    # Result formatting filters
    metadata_summarize_drop_empty_strings: bool = True
    metadata_summarize_exclude_fields: List[str] = ["config_name","qualified_name","ИсторияДанных","doc_description_embedding","description_embedding","object_summary_embedding","object_summary_search_text","object_summary_path","console_search_name","console_search_name_norm","console_search_synonym","console_search_synonym_norm","console_search_type","console_search_type_norm","console_search_section"]
    # Example JSON for .env:
    #   METADATA_SUMMARIZE_EXCLUDE_FIELD_VALUES={"ПолнотекстовыйПоиск":["Использовать"]}
    metadata_summarize_exclude_field_values: Dict[str, List[str]] = {}

    # Fulltext ranking thresholds for adaptive min_score (configurable via env):
    # - FT_MIN_SCORE_SHORT_TOKENS, FT_MIN_SCORE_SHORT_VALUE
    # - FT_MIN_SCORE_MEDIUM_TOKENS, FT_MIN_SCORE_MEDIUM_VALUE
    # - FT_MIN_SCORE_DEFAULT
    ft_min_score_short_tokens: int = 2
    ft_min_score_short_value: float = 2.0
    ft_min_score_medium_tokens: int = 5
    ft_min_score_medium_value: float = 1.0
    ft_min_score_default: float = 0.1

    # Vector similarity thresholds for adaptive filtering (configurable via env):
    # - VEC_MIN_SIM_SHORT_TOKENS, VEC_MIN_SIM_SHORT_VALUE
    # - VEC_MIN_SIM_MEDIUM_TOKENS, VEC_MIN_SIM_MEDIUM_VALUE
    # - VEC_MIN_SIM_DEFAULT
    vec_min_sim_short_tokens: int = 2
    vec_min_sim_short_value: float = 0.25
    vec_min_sim_medium_tokens: int = 5
    vec_min_sim_medium_value: float = 0.20
    vec_min_sim_default: float = 0.15
    
    # Code body settings
    # Maximum characters to return for a single routine body (to prevent huge responses)
    max_return_routine_body_length: int = 10000
    # Default LIMIT for routine search by doc_description (lower than general query_max_results to improve performance)
    routine_description_search_default_limit: int = 100

    # Hybrid search settings (combines fulltext + vector search when embeddings enabled)
    # Weights for hybrid search scoring (must sum to 1.0 for best results)
    hybrid_search_fulltext_weight: float = 0.3  # Weight for fulltext search component
    hybrid_search_vector_weight: float = 0.7    # Weight for vector search component
    # Oversampling factor for hybrid search (eff_k = factor × (limit + offset)).
    # Improves recall by fetching deeper candidates before hybrid ranking; recommended 2..3.
    hybrid_oversample_factor: int = 3
    # Cap for effective depth (eff_k) fetched from each leg to stabilize latency on big corpora
    hybrid_eff_k_cap: int = 300
    # Maximum distinct categories allowed in a single description search before it is rejected.
    # При description-поиске с categories=[...] vector ветка делает fan-out — один SEARCH-leg на категорию.
    # Лимит нужен, чтобы запрос с сотнями категорий не превратился в сотни SEARCH-запросов.
    vec_max_category_filters: int = 5

    # Hybrid fusion/normalization strategy toggles
    # - HYBRID_NORMALIZATION_STRATEGY: 'max' (default) or 'p95'
    # - HYBRID_FUSION_MODE: 'weighted' (default) or 'rrf'
    # - HYBRID_RRF_K: k-constant for RRF (typical 60)
    # - HYBRID_DYNAMIC_WEIGHTS_ENABLED: enable dynamic α/β per query
    # - HYBRID_ALPHA_MIN/HYBRID_ALPHA_MAX: clamp bounds for α (fulltext weight)
    hybrid_normalization_strategy: str = "p95"
    hybrid_fusion_mode: str = "weighted"
    hybrid_rrf_k: int = 60
    hybrid_dynamic_weights_enabled: bool = True
    hybrid_alpha_min: float = 0.2
    hybrid_alpha_max: float = 0.8
    
    # Metadata description search (fulltext по Справка/Синоним/Комментарий/name)
    # Default LIMIT for metadata description search (lower than general query_max_results to improve performance)
    metadata_description_search_default_limit: int = 100
    # Note: min_score uses adaptive thresholds (ft_min_score_*) same as code search

    # ====================
    # BSL code search (semantic search by routine body)
    # ====================
    # Master flag — turns on the whole subsystem (indexer + MCP tool + SQLite sidecar).
    enable_bsl_code_search: bool = True
    # Sub-flag for the embedding (vector) leg. When False, the indexer still builds the
    # SQLite/RLM sidecar and search falls back to RLM. Effective vector gate =
    # enable_bsl_code_search AND enable_bsl_code_embedding.
    enable_bsl_code_embedding: bool = False
    # Batch size for Phase B embedding API calls. Code units are heavier than
    # description texts, so this is intentionally separate from embedding_batch_size.
    # Each API call receives at most this many code unit texts.
    bsl_code_embedding_batch_size: int = 16
    # Аварийный выключатель scoped-удаления конфигурации из BSL sidecar.
    # False → удаление расширения идёт прежним путём (purge + полная
    # перестройка Phase A). Флаг влияет только на НОВЫЕ удаления: операция,
    # уже пересёкшая destructive-границу, доводится до конца в любом случае.
    bsl_config_delete_scoped_enabled: bool = True

    # Split / compression / prompt mode
    # Strategy name encodes per-routine slicing: small=whole routine,
    # large=ast-safe sliding window via tree-sitter-bsl.
    # Allowed: ast_safe_sliding_3600_720_min480, ast_safe_sliding_2200_440_min300.
    bsl_code_split_strategy: str = "ast_safe_sliding_3600_720_min480"
    # "none" = raw text only (default best-approach). Other values trigger lexical-dedup compressor.
    # Allowed: none, lexdedup_terms_cap1_lines_normprefix, lexdedup_cap1_nochainparts_lines_normprefix,
    # rawbelow1000_lexdedup_terms_cap1_lines_normprefix (short units <1000 chars stay raw, longer deduped),
    # rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix.
    bsl_code_compression_strategy: str = "none"
    # env-facing prompt mode (resolved to internal profile name via resolve_bsl_code_prompt_profile).
    # Allowed: none, auto, jina-code-nl2code, jina-v5-retrieval, jina-v4-code,
    # nomic-code-search, coderankembed-code-search, harrier-code-search,
    # f2llm-code-search, qwen3-code-search.
    bsl_code_embedding_prompt_mode: str = "auto"

    # Search behaviour
    bsl_code_vector_top_k: int = 50
    bsl_code_search_default_limit: int = 5

    # Search-visible coverage policy. Both settings can be flipped on/off at
    # runtime — coverage rebuild logic in BslCodeIndexer handles dynamics
    # (newly_hidden -> just update fingerprint; newly_visible -> Phase B delta).
    # Vectors are never deleted; defensive WHERE in vector cypher and source
    # SQL filters in RLM hide excluded scope from results.
    bsl_code_embedding_excluded_owner_categories: List[str] = Field(
        default_factory=list
    )
    bsl_code_search_exclude_regulated_reports: bool = True

    # Shared cross-encoder reranker layer (см. graphdb/reranker.py).
    # Активируется при непустом rerank_api_key. Используется и BSL, и object summary
    # консьюмерами через общий singleton; каждый консьюмер сам решает включён ли rerank
    # (*_rerank_enabled) и сколько кандидатов отправить (*_rerank_top_k).
    rerank_model: str = "cohere/rerank-4-fast"
    rerank_api_base: str = "https://openrouter.ai/api/v1"
    rerank_api_key: str = ""
    rerank_proxy: Optional[str] = None
    rerank_timeout_seconds: float = 60.0
    rerank_max_retries: int = 2

    # BSL-specific: только активация и размер пула; хвост за пределами top_k отбрасывается.
    bsl_code_rerank_enabled: bool = False
    bsl_code_rerank_top_k: int = 50

    # Reindex versioning — bump manually when the splitter or structural extractor changes.
    bsl_code_units_version: int = 1
    bsl_code_structural_extractor_version: int = 1

    # SQLite sidecar
    bsl_code_search_sqlite_path: str = "storage/search/bsl_code_search.sqlite"
    bsl_code_reindex_on_fingerprint_mismatch: bool = True
    # Страничный кэш sidecar. Умолчание SQLite — 2 МБ, что на базе в сотни мегабайт
    # даёт постоянный промах на записи в FTS-индексы. Память процесса, поэтому
    # настраивается: подсистема уже меняет память на пропускную способность через
    # workers / work_batch_max_mb / write_batch_units.
    bsl_code_sqlite_cache_mb: int = 64
    bsl_code_phase_a_write_batch_units: int = 1000
    bsl_code_phase_a_module_commit_batch: int = 100

    # Phase A streaming Neo4j fetch (lightweight pass for source_state_hash, then body batches).
    # ORDER BY rel_path, routine_id for module-boundary-aware streaming flush.
    bsl_code_routine_fetch_batch_size: int = 1000
    bsl_code_routine_prefetch_batches: int = 2

    # Phase A multiprocessing (CPU-bound: split + tokenize + structural extraction).
    bsl_code_phase_a_workers: int = 4
    bsl_code_phase_a_work_batch_routines: int = 100
    # Size-aware batch cap (sum(len(body)) in MB) — by analogy with neo4j_bsl_batch_max_mb
    # in BSL parsing pipeline. Packing is min(count_limit, byte_limit).
    bsl_code_phase_a_work_batch_max_mb: int = 16

    # Phase B async workers (I/O-bound: HTTP embedding + Neo4j write).
    # Partition by fts_rowid % total_workers. Sync embedding_service is bridged via asyncio.to_thread.
    bsl_code_phase_b_workers: int = 4
    # Phase B outer-round retry: total number of Phase B passes in a single run
    # (NOT "1 + retries"). 3 = the main pass plus up to two extra passes over the
    # remaining not-done units. 1 restores the old "one pass then failed" model.
    # Retry ownership (three distinct layers):
    #   - EMBEDDING_MAX_RETRIES fixes a single embedding HTTP request in a batch;
    #   - these rounds finish the remaining not-done units within the current run
    #     (both the full and the scoped/incremental Phase B paths);
    #   - the delta applier's FAILED_RETRY_QUEUED is the durable retry between
    #     incremental cycles.
    # A single knob is shared by both paths on purpose (scoped already has the
    # applier's durable retry). Backoff applies only between rounds and is
    # heartbeat-aware so a sustained outage cannot starve the scheduler lease.
    bsl_code_phase_b_max_rounds: int = 3
    bsl_code_phase_b_round_backoff_base_seconds: float = 10.0
    bsl_code_phase_b_round_backoff_max_seconds: float = 60.0
    bsl_code_phase_b_round_backoff_jitter_seconds: float = 1.0
    # Startup-only overlapped Phase A + Phase B. Overlap is enabled ONLY for the
    # startup BSL indexer (run_mode="startup"); scheduled/scoped/incremental keep
    # the sequential model above. Phase A stays independent of the embedding
    # endpoint: a dead endpoint never blocks or fails Phase A — the vector layer
    # just stays failed/degraded and is caught up by these rounds or next cycle.
    bsl_code_startup_phase_b_overlap_enabled: bool = True
    # Bounded queue between Phase A (producer) and the overlap controller. Full
    # queue → Phase A drops the notification (SQLite catch-up recovers it).
    bsl_code_startup_phase_b_overlap_queue_batches: int = 4
    # Drain-chunk granularity pulled from the queue before slicing into provider
    # sub-batches (<= bsl_code_embedding_batch_size). Only affects Phase A↔controller
    # overhead, not the provider request size.
    bsl_code_startup_phase_b_overlap_chunk_routines: int = 1000
    bsl_code_startup_phase_b_overlap_chunk_units: int = 2000
    # Startup Phase B outer-round retry policy (final catch-up). Long fixed window
    # because startup tolerates a slow endpoint recovery; scheduled keeps 3 rounds.
    bsl_code_startup_phase_b_max_rounds: int = 12
    bsl_code_startup_phase_b_round_backoff_mode: str = "fixed"  # "fixed" | "exponential"
    bsl_code_startup_phase_b_round_backoff_seconds: float = 300.0
    bsl_code_startup_phase_b_round_backoff_jitter_seconds: float = 10.0
    # Poll timeout (seconds) waiting for vec_bsl_code_unit to reach ONLINE before
    # committing vector_status=ready. On timeout: status=failed, retry next cycle.
    bsl_code_vector_index_online_timeout_seconds: float = 600.0
    # Old SQLite epoch is kept this many seconds after pending->current switch so that
    # long-running search requests holding a pinned read_epoch do not lose their data.
    # None = auto-derive: max(60, int(embedding_timeout_seconds * 1.2)).
    sqlite_epoch_retention_seconds: Optional[int] = None

    @property
    def effective_sqlite_epoch_retention_seconds(self) -> int:
        if self.sqlite_epoch_retention_seconds is not None:
            return int(self.sqlite_epoch_retention_seconds)
        return max(60, int(self.embedding_timeout_seconds * 1.2))

    # ====================
    # Object summary
    # ====================
    object_summary_enabled: bool = False
    object_summary_generation_mode: str = "manual"  # auto | manual
    # Управляет только кнопкой "Обновить" в веб-консоли. "Создать" доступна
    # пока есть OBJECT_SUMMARY_ENABLED=true. Default true: при выключенном
    # режиме существующая сводка не перегенерируется вручную.
    object_summary_manual_regeneration_enabled: bool = True
    object_summary_categories: List[str] = Field(default_factory=lambda: [
        "Справочники", "Документы", "РегистрыСведений", "РегистрыНакопления",
        "Обработки", "HTTPСервисы", "БизнесПроцессы", "Задачи",
    ])
    object_summary_profile_size_policy: str = "medium"  # small | medium | large

    # Object summary LLM (separate channel from EMBEDDING_* / RERANK_*)
    object_summary_model: str = "openai/gpt-5.4-mini"
    object_summary_llm_api_base: Optional[str] = "https://openrouter.ai/api/v1"
    object_summary_llm_api_key: Optional[str] = None
    object_summary_llm_proxy: Optional[str] = None
    object_summary_llm_timeout: float = 300.0
    object_summary_llm_temperature: float = 0.0

    # Generation workers
    object_summary_generation_workers: int = 2
    object_summary_generation_batch_size: int = 10
    object_summary_generation_max_retries: int = 3
    # Object-level retry: повторяет _generate_for_object целиком после битого JSON,
    # output limit, validator fail. Внутри ObjectSummaryLLM остаётся свой retry для
    # transient API (max_retries). 1 = повторных кругов нет.
    object_summary_generation_attempts: int = 3
    object_summary_reconcile_batch_size: int = 500
    object_summary_regenerate_on_profile_version_change: bool = False

    # Embedding workers for summary (use existing EMBEDDING_* for the embedding call itself)
    object_summary_embedding_workers: int = 4
    object_summary_embedding_batch_size: int = 8

    # Extensions
    object_summary_generate_for_extensions: bool = False
    object_summary_extension_names: List[str] = Field(default_factory=lambda: ["*"])
    object_summary_extension_object_scope: str = "own"  # own | all

    # Object summary rerank: только активация и размер пула; хвост сохраняется
    # (reranked_head + head_without_rerank_score + original_tail) для pagination.
    object_summary_rerank_enabled: bool = False
    object_summary_rerank_top_k: int = 50

    # Metadata description rerank: только активация и размер пула.
    metadata_description_rerank_enabled: bool = False
    metadata_description_rerank_top_k: int = 50

    # Routine description rerank: только активация и размер пула.
    routine_description_rerank_enabled: bool = False
    routine_description_rerank_top_k: int = 50

    # Runtime usage aggregates (per-process SQLite, see app/runtime_metrics.py).
    # Relative path is used as-is, like incremental_loading_state_path.
    runtime_metrics_sqlite_path: str = "storage/runtime/runtime_metrics.sqlite"

    @property
    def object_summary_directory(self) -> Path:
        return self.data_directory / "object_summary"

    @field_validator("object_summary_generation_mode")
    @classmethod
    def _validate_summary_mode(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"auto", "manual"}:
            raise ValueError(f"OBJECT_SUMMARY_GENERATION_MODE must be 'auto' or 'manual', got {v!r}")
        return v

    @field_validator("object_summary_profile_size_policy")
    @classmethod
    def _validate_summary_size(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"small", "medium", "large"}:
            raise ValueError(f"OBJECT_SUMMARY_PROFILE_SIZE_POLICY must be 'small'|'medium'|'large', got {v!r}")
        return v

    @field_validator("object_summary_extension_object_scope")
    @classmethod
    def _validate_summary_ext_scope(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in {"own", "all"}:
            raise ValueError(f"OBJECT_SUMMARY_EXTENSION_OBJECT_SCOPE must be 'own' or 'all', got {v!r}")
        return v


def resolve_xml_standard_attributes_mode(mode: str) -> tuple[bool, bool]:
    """Map XML_STANDARD_ATTRIBUTES_MODE to parser flags.

    Returns (materialize_standard_attrs, preserve_listed_standard_attrs).
    Raises ValueError on an unknown mode so XML loading fails early with a clear
    message instead of silently producing incomplete data.
    """
    m = (mode or "").strip().lower()
    if m == "listed":
        return False, True
    if m == "materialized":
        return True, True
    raise ValueError(
        f"Invalid XML_STANDARD_ATTRIBUTES_MODE={mode!r}; expected 'listed' or 'materialized'"
    )


# Create global settings instance
settings = Settings()

# 1C Configuration name loaded from Neo4j database at startup
# This is set dynamically in server.py after metadata is loaded
onec_config_name: str | None = None
