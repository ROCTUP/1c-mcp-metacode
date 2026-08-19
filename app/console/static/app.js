'use strict';

const cfg = window.__CONSOLE_CONFIG__;
const API_PREFIX = cfg.apiPrefix;
const CURRENT_USER = cfg.auth || {};
const AUTH_TOKEN_PARAM = CURRENT_USER.tokenParam || '';
const AUTH_TOKEN = CURRENT_USER.token || '';
const IS_ADMIN = CURRENT_USER.role === 'admin';
const AUTH_USER_KEY = String(CURRENT_USER.userId || CURRENT_USER.login || 'anonymous');
const CONSOLE_PATH = normalizePath(cfg.consolePath || '/console');
const APP_VERSION = String(cfg.appVersion || '2.0.1');
const AGENT_ENABLED = Boolean(cfg.agentEnabled);
const AGENT_LLM = cfg.agentLlm || {};

const LABELS = {
  TotalNodes: 'Total Nodes',
  Relationships: 'Relationships',
  Project: 'Project',
  Configuration: 'Configuration',
  MetadataCategory: 'Metadata Categories',
  MetadataObject: 'Metadata Objects',
  Attribute: 'Attributes',
  Form: 'Forms',
  FormControl: 'Form Controls',
  FormEvent: 'Form Events',
  FormEventAction: 'Form Event Actions',
  FormAttribute: 'Form Attributes',
  Command: 'Commands',
  Layout: 'Layouts',
  TabularPart: 'Tabular Parts',
  Resource: 'Resources',
  Dimension: 'Dimensions',
  EnumValue: 'Enum Values',
  Characteristic: 'Characteristics',
  UrlTemplate: 'URL Templates',
  UrlMethod: 'URL Methods',
  AccountingFlag: 'Accounting Flags',
  DimensionAccountingFlag: 'Dim. Acc. Flags',
  EventSubscription: 'Event Subscriptions',
  Module: 'Modules',
  Routine: 'Routines',
  JournalGraph: 'Journal Graphs',
};

const PINNED = new Set(['TotalNodes', 'Relationships', 'Project']);
const CATEGORY_ORDER = [
  'Константы', 'Справочники', 'Документы', 'ЖурналыДокументов',
  'Перечисления', 'Отчеты', 'Обработки', 'ПланыВидовХарактеристик',
  'ПланыСчетов', 'ПланыВидовРасчета', 'РегистрыСведений',
  'РегистрыНакопления', 'РегистрыБухгалтерии', 'РегистрыРасчета',
  'БизнесПроцессы', 'Задачи', 'ВнешниеИсточникиДанных',
];
const COMMON_CATEGORY_ORDER = [
  'Подсистемы', 'ОбщиеМодули', 'ПараметрыСеанса', 'Роли',
  'ОбщиеРеквизиты', 'ПланыОбмена', 'КритерииОтбора',
  'ПодпискиНаСобытия', 'РегламентныеЗадания', 'Боты',
  'ФункциональныеОпции', 'ПараметрыФункциональныхОпций',
  'ОпределяемыеТипы', 'ХранилищаНастроек', 'ОбщиеКоманды',
  'ГруппыКоманд', 'ОбщиеФормы', 'Интерфейсы', 'ОбщиеМакеты',
  'ОбщиеКартинки', 'XDTOПакеты', 'WebСервисы', 'HTTPСервисы',
  'WSСсылки', 'WebSocketКлиенты', 'СервисыИнтеграции',
  'ЭлементыСтиля', 'Стили', 'Языки',
];
const CATEGORY_ORDER_ALIASES = Object.freeze({
  'пакетыxdto': 'xdtoпакеты',
});
const NESTED_CATEGORY_PARENT = Object.freeze({
  'нумераторыдокументов': 'документы',
});
const NESTED_CATEGORY_LABELS = Object.freeze({
  'нумераторыдокументов': 'Нумераторы',
});
const CATEGORY_DISPLAY_LABELS = Object.freeze({
  'бизнеспроцессы': 'Бизнес-процессы',
  'внешниеисточникиданных': 'Внешние источники данных',
  'webсервисы': 'Web-сервисы',
  'httpсервисы': 'HTTP-сервисы',
  'websocketклиенты': 'WebSocket-клиенты',
  'wsссылки': 'WS-ссылки',
  'группыкоманд': 'Группы команд',
  'журналыдокументов': 'Журналы документов',
  'критерииотбора': 'Критерии отбора',
  'общиекартинки': 'Общие картинки',
  'общиекоманды': 'Общие команды',
  'общиемакеты': 'Общие макеты',
  'общиемодули': 'Общие модули',
  'общиереквизиты': 'Общие реквизиты',
  'общиеформы': 'Общие формы',
  'определяемыетипы': 'Определяемые типы',
  'пакетыxdto': 'XDTO-пакеты',
  'параметрысеанса': 'Параметры сеанса',
  'параметрыфункциональныхопций': 'Параметры функциональных опций',
  'планывидоврасчета': 'Планы видов расчета',
  'планывидовхарактеристик': 'Планы видов характеристик',
  'планыобмена': 'Планы обмена',
  'планысчетов': 'Планы счетов',
  'подпискинасобытия': 'Подписки на события',
  'регистрыбухгалтерии': 'Регистры бухгалтерии',
  'регистрынакопления': 'Регистры накопления',
  'регистрырасчета': 'Регистры расчета',
  'регистрысведений': 'Регистры сведений',
  'регламентныезадания': 'Регламентные задания',
  'сервисыинтеграции': 'Сервисы интеграции',
  'функциональныеопции': 'Функциональные опции',
  'хранилищанастроек': 'Хранилища настроек',
  'элементыстиля': 'Элементы стиля',
});
const COMMON_CATEGORY_SET = new Set(COMMON_CATEGORY_ORDER.map(categoryOrderKey));

// <tree-icons>
const TREE_ICON_FILES = Object.freeze({
  accounting_register: 'accounting_register.png',
  accumulation_register: 'accumulation_register.png',
  bot: 'bot.png',
  business_process: 'business_process.png',
  calculation_register: 'calculation_register.png',
  catalog: 'catalog.png',
  chart_of_accounts: 'chart_of_accounts.png',
  chart_of_calculation_types: 'chart_of_calculation_types.png',
  chart_of_characteristic_types: 'chart_of_characteristic_types.png',
  command_group: 'command_group.png',
  command_interface: 'command_interface.png',
  common: 'common.png',
  common_attribute: 'common_attribute.png',
  common_command: 'common_command.png',
  common_form: 'common_form.png',
  common_module: 'common_module.png',
  common_picture: 'common_picture.png',
  common_template: 'common_template.png',
  configuration: 'configuration.png',
  constant: 'constant.png',
  data_processor: 'data_processor.png',
  defined_types: 'defined_types.png',
  document: 'document.png',
  document_journal: 'document_journal.png',
  document_numerator: 'document_numerator.png',
  enum: 'enum.png',
  event_subscription: 'event_subscription.png',
  exchange_plan: 'exchange_plan.png',
  external_data_source: 'external_data_source.png',
  filter_criterion: 'filter_criterion.png',
  functional_option: 'functional_option.png',
  functional_option_parameter: 'functional_option_parameter.png',
  http: 'http.png',
  information_register: 'information_register.png',
  integration_service: 'integration_service.png',
  language: 'language.png',
  report: 'report.png',
  role: 'role.png',
  scheduled_job: 'scheduled_job.png',
  sequence: 'sequence.png',
  session_parameter: 'session_parameter.png',
  settings_storage: 'settings_storage.png',
  style: 'style.png',
  style_item: 'style_item.png',
  subsystem: 'subsystem.png',
  task: 'task.png',
  web_service: 'web_service.png',
  web_socket_client: 'web_socket_client.png',
  ws_reference: 'ws_reference.png',
  xdto_package: 'xdto_package.png',
  attribute: 'attribute.png',
  tabular_section: 'tabular_section.png',
  register_resource: 'register_resource.png',
  register_dimension: 'register_dimension.png',
  form: 'form.png',
  command: 'command.png',
  template: 'template.png',
  enum_value: 'enum_value.png',
  form_control_button: 'form_control_button.png',
  form_control_check: 'form_control_check.png',
  form_control_column: 'form_control_column.png',
  form_control_command_panel: 'form_control_command_panel.png',
  form_control_decoration: 'form_control_decoration.png',
  form_control_field: 'form_control_field.png',
  form_elements: 'form_elements.png',
  form_control_group: 'form_control_group.png',
  form_control_radio: 'form_control_radio.png',
  form_control_search: 'form_control_search.png',
  form_control_table: 'form_control_table.png',
  predefined: 'predefined.png',
  project: 'project.png',
  module: 'module.png',
  standard_attribute: 'standard_attribute.png',
  journal_graph: 'journal_graph.png',
});

const CATEGORY_ICON_MAP = Object.freeze({
  'Константы': 'constant',
  'Справочники': 'catalog',
  'Документы': 'document',
  'ЖурналыДокументов': 'document_journal',
  'Журналы документов': 'document_journal',
  'НумераторыДокументов': 'document_numerator',
  'Нумераторы документов': 'document_numerator',
  'Перечисления': 'enum',
  'Отчеты': 'report',
  'Обработки': 'data_processor',
  'ПланыВидовХарактеристик': 'chart_of_characteristic_types',
  'Планы видов характеристик': 'chart_of_characteristic_types',
  'ПланыСчетов': 'chart_of_accounts',
  'Планы счетов': 'chart_of_accounts',
  'ПланыВидовРасчета': 'chart_of_calculation_types',
  'Планы видов расчета': 'chart_of_calculation_types',
  'РегистрыСведений': 'information_register',
  'Регистры сведений': 'information_register',
  'РегистрыНакопления': 'accumulation_register',
  'Регистры накопления': 'accumulation_register',
  'РегистрыБухгалтерии': 'accounting_register',
  'Регистры бухгалтерии': 'accounting_register',
  'РегистрыРасчета': 'calculation_register',
  'Регистры расчета': 'calculation_register',
  'БизнесПроцессы': 'business_process',
  'Бизнес-процессы': 'business_process',
  'Задачи': 'task',
  'ВнешниеИсточникиДанных': 'external_data_source',
  'Внешние источники данных': 'external_data_source',
  'Последовательности': 'sequence',
  'Подсистемы': 'subsystem',
  'ОбщиеМодули': 'common_module',
  'Общие модули': 'common_module',
  'ПараметрыСеанса': 'session_parameter',
  'Параметры сеанса': 'session_parameter',
  'Роли': 'role',
  'ОбщиеРеквизиты': 'common_attribute',
  'Общие реквизиты': 'common_attribute',
  'ПланыОбмена': 'exchange_plan',
  'Планы обмена': 'exchange_plan',
  'КритерииОтбора': 'filter_criterion',
  'Критерии отбора': 'filter_criterion',
  'ПодпискиНаСобытия': 'event_subscription',
  'Подписки на события': 'event_subscription',
  'РегламентныеЗадания': 'scheduled_job',
  'Регламентные задания': 'scheduled_job',
  'Боты': 'bot',
  'ФункциональныеОпции': 'functional_option',
  'Функциональные опции': 'functional_option',
  'ПараметрыФункциональныхОпций': 'functional_option_parameter',
  'Параметры функциональных опций': 'functional_option_parameter',
  'ОпределяемыеТипы': 'defined_types',
  'Определяемые типы': 'defined_types',
  'ХранилищаНастроек': 'settings_storage',
  'Хранилища настроек': 'settings_storage',
  'ОбщиеКоманды': 'common_command',
  'Общие команды': 'common_command',
  'ГруппыКоманд': 'command_group',
  'Группы команд': 'command_group',
  'ОбщиеФормы': 'common_form',
  'Общие формы': 'common_form',
  'Интерфейсы': 'command_interface',
  'ОбщиеМакеты': 'common_template',
  'Общие макеты': 'common_template',
  'ОбщиеКартинки': 'common_picture',
  'Общие картинки': 'common_picture',
  'XDTOПакеты': 'xdto_package',
  'XDTO-пакеты': 'xdto_package',
  'ПакетыXDTO': 'xdto_package',
  'WebСервисы': 'web_service',
  'Web-сервисы': 'web_service',
  'HTTPСервисы': 'http',
  'HTTP-сервисы': 'http',
  'WSСсылки': 'ws_reference',
  'WS-ссылки': 'ws_reference',
  'WebSocketКлиенты': 'web_socket_client',
  'WebSocket-клиенты': 'web_socket_client',
  'СервисыИнтеграции': 'integration_service',
  'Сервисы интеграции': 'integration_service',
  'ЭлементыСтиля': 'style_item',
  'Элементы стиля': 'style_item',
  'Стили': 'style',
  'Языки': 'language',
});

function categoryIconName(categoryName) {
  return CATEGORY_ICON_MAP[categoryName] || 'common';
}

function displayConfigName(name) {
  return String(name || '').replace(/\$ext\$/g, '');
}

function treeIconHtml(iconName) {
  const file = TREE_ICON_FILES[iconName] || TREE_ICON_FILES.common;
  return `<img class="tree-icon" src="${CONSOLE_PATH}/static/icons/${file}" alt="" aria-hidden="true" loading="lazy" onerror="this.remove()">`;
}

function extensionObjectState(item) {
  const ownership = String(item?.ownership || '').toLowerCase();
  if (item?.is_adopted || ownership.includes('заимств')) return 'adopted';
  return '';
}

function isBaseConfigName(configName) {
  const config = (analysisState.tree?.configurations || []).find((item) => item.name === configName);
  return Boolean(config && !config.is_extension);
}

function extensionDisplayNames(item) {
  const names = (item?.extension_names || [])
    .map((name) => displayConfigName(name))
    .filter(Boolean);
  return [...new Set(names)];
}

function baseAdoptionMarkerHtml(item, configName) {
  if (!isBaseConfigName(configName)) return '';
  const names = extensionDisplayNames(item);
  const count = names.length || Number(item?.extension_adoptions || 0);
  if (!count) return '';
  const suffix = count > 1 ? String(count) : '';
  const title = names.length
    ? `Заимствован в ${names.length === 1 ? 'расширении' : 'расширениях'}: ${names.join(', ')}`
    : 'Заимствован в расширении';
  return `<span class="tree-adoption-marker" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}">⧉${suffix}</span>`;
}

function treeIconWithStateHtml(iconName, state) {
  if (!state) return treeIconHtml(iconName);
  return `
    <span class="tree-icon-wrap tree-icon--${escapeHtml(state)}">
      ${treeIconHtml(iconName)}
      <img class="tree-icon-state" src="${CONSOLE_PATH}/static/icons/extension_ovr.png" alt="" aria-hidden="true" loading="lazy">
    </span>
  `;
}
// </tree-icons>
const STRUCTURE_TITLES = {
  standard_attributes: 'Стандартные реквизиты',
  attributes: 'Реквизиты',
  tabular_parts: 'Табличные части',
  tabular_part_attributes: 'Реквизиты',
  resources: 'Ресурсы',
  dimensions: 'Измерения',
  forms: 'Формы',
  commands: 'Команды',
  layouts: 'Макеты',
  journal_graphs: 'Графы',
  enum_values: 'Значения',
  predefined: 'Предопределенные',
  modules: 'Модули',
  form_attributes: 'Реквизиты форм',
  form_controls: 'Элементы форм',
};

const SEARCH_TYPE_OPTIONS = [
  ['objects', 'Объекты'],
  ['attributes', 'Реквизиты'],
  ['standard_attributes', 'Стандартные реквизиты'],
  ['tabular_parts', 'Табличные части'],
  ['tabular_part_attributes', 'Реквизиты табличных частей'],
  ['resources', 'Ресурсы'],
  ['dimensions', 'Измерения'],
  ['forms', 'Формы'],
  ['commands', 'Команды'],
  ['layouts', 'Макеты'],
  ['journal_graphs', 'Графы'],
  ['enum_values', 'Значения перечислений'],
  ['predefined', 'Предопределенные'],
  ['modules', 'Модули'],
  ['form_attributes', 'Реквизиты форм'],
  ['form_controls', 'Элементы форм'],
];

const SEARCH_TYPE_COLUMNS = [
  ['objects', 'attributes', 'standard_attributes', 'tabular_parts', 'tabular_part_attributes'],
  ['dimensions', 'resources', 'commands', 'layouts', 'modules'],
  ['journal_graphs', 'enum_values', 'predefined', 'forms', 'form_attributes', 'form_controls'],
];

const SEARCH_FIELD_OPTIONS = [
  ['name', 'Имя'],
  ['synonym', 'Синоним / заголовок'],
  ['comment', 'Комментарий'],
  ['type', 'Тип / действие'],
  ['category', 'Категория'],
  ['config', 'Конфигурация'],
  ['path', 'Полный путь'],
];
const SEARCH_DEBOUNCE_MS = 2000;
const ANALYSIS_STATE_STORAGE_KEY = 'metacode.console.analysis.state';
const AGENT_SESSION_STORAGE_KEY = `metacode.console.agent.session.${AUTH_USER_KEY}`;
const AGENT_ACTIVE_CHAT_STORAGE_KEY = `metacode.console.agent.activeChat.${AUTH_USER_KEY}`;
const AGENT_TRANSCRIPT_STORAGE_PREFIX = `metacode.console.agent.transcript.${AUTH_USER_KEY}.`;
const AGENT_USAGE_STORAGE_PREFIX = `metacode.console.agent.usage.${AUTH_USER_KEY}.`;
const AGENT_SEND_CONTEXT_STORAGE_KEY = 'metacode.console.agent.sendContext';
const AGENT_RUNNING_CHATS_REFRESH_MS = 3000;
const ANALYSIS_FORM_TREE_ENABLED = true;
const MODULE_UNIT_COLORS = ['#38bdf8', '#34d399', '#f59e0b', '#c084fc', '#fb7185', '#22d3ee'];

const analysisState = {
  tree: null,
  categoryCache: new Map(),
  objectCache: new Map(),
  formTreeCache: new Map(),
  moduleCache: new Map(),
  moduleUnitsCache: new Map(),
  formTreeEnabled: ANALYSIS_FORM_TREE_ENABLED,
  moduleUnitsVisible: false,
  selectedRef: null,
  selectedKind: 'project',
  selectedConfig: null,
  selectedCategory: null,
  selectedSection: null,
  selectedObject: null,
  selectedNode: null,
  selectedModule: null,
  selectedModuleId: null,
  selectedModuleOwner: null,
  selectedModuleType: null,
  relationships: null,
  rightConditions: new Map(),
  rightConditionSeq: 0,
  backStack: [],
  tab: 'summary',
  searchQuery: '',
  searchSeq: 0,
  searchTimer: null,
  searchData: null,
  searchVisibleCount: 0,
  searchPrefetching: false,
  searchLoadMorePending: false,
  searchConfig: '',
  searchTypes: SEARCH_TYPE_OPTIONS.map(([value]) => value),
  searchFields: SEARCH_FIELD_OPTIONS.map(([value]) => value),
  pendingSearchVisibleCount: 0,
  pendingRestore: null,
};

const agentState = {
  chatId: '',
  chats: [],
  chatListOpen: false,
  chatsInitialized: false,
  loadingChat: false,
  pendingChatRequest: 0,
  llmProfiles: Array.isArray(AGENT_LLM.profiles) ? AGENT_LLM.profiles : [],
  llmMode: String(AGENT_LLM.mode || 'single_env'),
  llmProfileId: String(AGENT_LLM.defaultProfileId || ''),
  sessionId: readAgentSessionId(),
  messages: [],
  busy: false,
  runningByChat: {},
  chatViews: {},
  activeTurnId: '',
  streamController: null,
  streamRunId: 0,
  runningRefreshTimer: null,
  lastEventSeqByTurn: {},
  activeAssistantMessageEl: null,
  activeAssistantTextEl: null,
  activeAssistantReasoningEl: null,
  activeAssistantPlanEl: null,
  activeAssistantStepsEl: null,
  pendingToolIds: [],
  toolSeq: 0,
  activeAnswerRaw: '',
  activeAnswer: '',
  activeReasoning: '',
  autoScroll: true,
  persistTimer: null,
  usage: null,
};

const mcpToolsState = {
  tools: [],
  activeToolName: '',
  activeTab: 'description',
  modalFullscreen: false,
  modalFrame: null,
  drag: null,
};

const agentToolModalState = {
  frame: null,
  drag: null,
};

const runtimeUsageState = {
  scope: 'all',
  data: null,
};

const consoleUsersState = {
  users: [],
  tokenToShow: '',
  urlToShow: '',
  tokenLogin: '',
};

function $(id) { return document.getElementById(id); }

function normalizePath(path) {
  if (!path || path === '/') return '';
  return path.replace(/\/+$/, '');
}

function withToken(path, params = {}) {
  const url = new URL(path, window.location.origin);
  if (AUTH_TOKEN && AUTH_TOKEN_PARAM) url.searchParams.set(AUTH_TOKEN_PARAM, AUTH_TOKEN);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, value);
    }
  }
  return `${url.pathname}${url.search}`;
}

function createAgentSessionId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  return `agent-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function readAgentSessionId() {
  try {
    const saved = localStorage.getItem(AGENT_SESSION_STORAGE_KEY);
    if (saved) return saved;
  } catch {
    // localStorage can be blocked in hardened browser contexts.
  }
  const sessionId = createAgentSessionId();
  writeAgentSessionId(sessionId);
  return sessionId;
}

function writeAgentSessionId(sessionId) {
  try {
    localStorage.setItem(AGENT_SESSION_STORAGE_KEY, sessionId);
  } catch {
    // Best effort; backend session still works for the current page lifetime.
  }
}

function readActiveAgentChatId() {
  try {
    return localStorage.getItem(AGENT_ACTIVE_CHAT_STORAGE_KEY) || '';
  } catch {
    return '';
  }
}

function writeActiveAgentChatId(chatId) {
  try {
    if (chatId) {
      localStorage.setItem(AGENT_ACTIVE_CHAT_STORAGE_KEY, chatId);
    } else {
      localStorage.removeItem(AGENT_ACTIVE_CHAT_STORAGE_KEY);
    }
  } catch {
    // Best effort only.
  }
}

function readAgentSendContextEnabled() {
  try {
    return localStorage.getItem(AGENT_SEND_CONTEXT_STORAGE_KEY) !== '0';
  } catch {
    return true;
  }
}

function writeAgentSendContextEnabled(enabled) {
  try {
    localStorage.setItem(AGENT_SEND_CONTEXT_STORAGE_KEY, enabled ? '1' : '0');
  } catch {
    // Best effort only.
  }
}

function agentTranscriptStorageKey(sessionId = agentState.sessionId) {
  return `${AGENT_TRANSCRIPT_STORAGE_PREFIX}${sessionId || 'default'}`;
}

function agentUsageStorageKey(sessionId = agentState.sessionId) {
  return `${AGENT_USAGE_STORAGE_PREFIX}${sessionId || 'default'}`;
}

function readAgentTranscript(sessionId = agentState.sessionId) {
  try {
    const raw = localStorage.getItem(agentTranscriptStorageKey(sessionId));
    if (!raw) return '';
    const payload = JSON.parse(raw);
    return typeof payload?.html === 'string' ? payload.html : '';
  } catch {
    return '';
  }
}

function readAgentUsage(sessionId = agentState.sessionId) {
  try {
    const raw = localStorage.getItem(agentUsageStorageKey(sessionId));
    if (!raw) return null;
    const payload = JSON.parse(raw);
    return payload && typeof payload.usage === 'object' ? payload.usage : null;
  } catch {
    return null;
  }
}

function removeAgentTranscript(sessionId = agentState.sessionId) {
  try {
    localStorage.removeItem(agentTranscriptStorageKey(sessionId));
  } catch {
    // Best effort only.
  }
}

function removeAgentUsage(sessionId = agentState.sessionId) {
  try {
    localStorage.removeItem(agentUsageStorageKey(sessionId));
  } catch {
    // Best effort only.
  }
}

function persistAgentUsage(usage = agentState.usage, sessionId = agentState.sessionId) {
  if (!AGENT_ENABLED || !usage) return;
  try {
    localStorage.setItem(agentUsageStorageKey(sessionId), JSON.stringify({
      version: 1,
      session_id: sessionId,
      updated_at: Date.now(),
      usage,
    }));
  } catch {
    // Best effort only.
  }
}

function persistAgentTranscriptNow() {
  if (!AGENT_ENABLED) return;
  const body = $('agent-body');
  if (!body) return;
  try {
    const onlyEmptyState = body.children.length === 1
      && body.firstElementChild?.matches('.agent-empty, .agent-disabled');
    if (onlyEmptyState || !body.innerHTML.trim()) {
      removeAgentTranscript();
      return;
    }
    localStorage.setItem(agentTranscriptStorageKey(), JSON.stringify({
      version: 1,
      session_id: agentState.sessionId,
      updated_at: Date.now(),
      html: body.innerHTML,
    }));
  } catch {
    // localStorage quota/private mode should not break the console.
  }
}

function scheduleAgentTranscriptPersist() {
  if (!AGENT_ENABLED) return;
  if (agentState.persistTimer) clearTimeout(agentState.persistTimer);
  agentState.persistTimer = setTimeout(() => {
    agentState.persistTimer = null;
    persistAgentTranscriptNow();
  }, 120);
}

function restoreAgentTranscript() {
  const body = $('agent-body');
  if (!body || !AGENT_ENABLED) return false;
  const html = readAgentTranscript();
  if (!html.trim()) return false;
  body.innerHTML = html;
  body.querySelectorAll('.agent-message-assistant.is-streaming').forEach((node) => {
    node.classList.remove('is-streaming');
  });
  if (window.Markdown && typeof window.Markdown.installCopyHandlerOnce === 'function') {
    window.Markdown.installCopyHandlerOnce();
  }
  renderAgentMermaid(body);
  scrollAgentToBottom(true);
  return true;
}

function isAgentBodyCacheable(body = $('agent-body')) {
  if (!body || !body.innerHTML.trim()) return false;
  if (body.children.length === 1 && body.firstElementChild?.matches('.agent-empty')) {
    const text = body.firstElementChild.textContent || '';
    return !/Загрузка/.test(text);
  }
  return !body.querySelector('.agent-disabled');
}

function cacheAgentCurrentView(chatId = agentState.chatId) {
  const id = String(chatId || '');
  const body = $('agent-body');
  if (!id || !AGENT_ENABLED || !isAgentBodyCacheable(body)) return;
  const running = agentState.runningByChat[id] || null;
  agentState.chatViews[id] = {
    html: body.innerHTML,
    scrollTop: body.scrollTop,
    usage: agentState.usage,
    activeTurnId: agentState.activeTurnId || running?.turnId || '',
    updatedAt: Date.now(),
  };
}

function invalidateAgentChatView(chatId) {
  const id = String(chatId || '');
  if (!id) return;
  delete agentState.chatViews[id];
}

function agentCachedHtmlHasStreaming(html) {
  return /agent-message-assistant[^"]*\bis-streaming\b/.test(String(html || ''));
}

function restoreAgentActiveRunStateFromDom() {
  clearAgentActiveRunState();
  const body = $('agent-body');
  if (!body) return false;
  const streamingMessages = [...body.querySelectorAll('.agent-message-assistant.is-streaming')];
  const message = streamingMessages[streamingMessages.length - 1] || null;
  if (!message) return false;

  agentState.activeAssistantMessageEl = message;
  agentState.activeAssistantTextEl = message.querySelector('.agent-message-text');
  agentState.activeAssistantReasoningEl = message.querySelector('.agent-reasoning');
  agentState.activeAssistantPlanEl = message.querySelector('.agent-plan');
  agentState.activeAssistantStepsEl = message.querySelector('.agent-tool-steps');
  agentState.activeAnswerRaw = message.getAttribute('data-raw-markdown') || '';
  agentState.activeAnswer = agentState.activeAnswerRaw;
  const reasoningBody = agentState.activeAssistantReasoningEl?.querySelector('.agent-reasoning-body');
  agentState.activeReasoning = agentState.activeAssistantReasoningEl?.getAttribute('data-raw-reasoning')
    || reasoningBody?.textContent
    || '';

  const pendingRows = agentState.activeAssistantStepsEl
    ? [...agentState.activeAssistantStepsEl.querySelectorAll('.agent-tool-row:not(.agent-tool-done):not(.agent-tool-error)')]
    : [];
  agentState.pendingToolIds = pendingRows
    .map((row) => row.dataset.toolId || '')
    .filter(Boolean);
  const maxToolSeq = [...body.querySelectorAll('[data-tool-id^="tool-"]')]
    .map((row) => Number(String(row.dataset.toolId || '').replace(/^tool-/, '')))
    .filter(Number.isFinite)
    .reduce((max, value) => Math.max(max, value), 0);
  agentState.toolSeq = maxToolSeq;
  return true;
}

function restoreAgentCachedView(chatId) {
  const id = String(chatId || '');
  const body = $('agent-body');
  const view = id ? agentState.chatViews[id] : null;
  if (!body || !view?.html) return false;
  const running = agentState.runningByChat[id] || null;
  const hasStreaming = agentCachedHtmlHasStreaming(view.html);
  if ((hasStreaming && !running) || (running && !hasStreaming)) {
    invalidateAgentChatView(id);
    return false;
  }

  body.innerHTML = view.html;
  agentState.usage = view.usage || readAgentUsage(id) || null;
  agentState.activeTurnId = view.activeTurnId || running?.turnId || '';
  renderAgentUsage(agentState.usage);
  restoreAgentActiveRunStateFromDom();
  if (window.Markdown && typeof window.Markdown.installCopyHandlerOnce === 'function') {
    window.Markdown.installCopyHandlerOnce();
  }
  renderAgentMermaid(body);
  window.requestAnimationFrame(() => {
    body.scrollTop = Math.min(Number(view.scrollTop || 0), Math.max(0, body.scrollHeight - body.clientHeight));
  });
  return true;
}

async function fetchAgentChats() {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats`));
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function createAgentChat() {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ llm_profile_id: normalizeAgentLlmProfileId(agentState.llmProfileId) }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function fetchAgentChatDetail(chatId) {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}`));
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function deleteAgentChat(chatId) {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}`), {
    method: 'DELETE',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function patchAgentChatProfile(chatId, profileId) {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}`), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ llm_profile_id: profileId }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function startAgentTurn(chatId, message, context, llmProfileId) {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}/turns`), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      context,
      llm_profile_id: llmProfileId,
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

async function stopAgentTurn(chatId, turnId) {
  const res = await fetch(withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(turnId)}/stop`), {
    method: 'POST',
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
  }
  return res.json();
}

function agentLlmProfileById(profileId) {
  const id = String(profileId || '');
  return agentState.llmProfiles.find((profile) => String(profile.id || '') === id) || null;
}

function normalizeAgentLlmProfileId(profileId) {
  const profiles = agentState.llmProfiles;
  if (!profiles.length) return String(profileId || '');
  const id = String(profileId || '');
  if (agentLlmProfileById(id)) return id;
  const defaultId = String(AGENT_LLM.defaultProfileId || '');
  if (agentLlmProfileById(defaultId)) return defaultId;
  return String(profiles[0]?.id || '');
}

function currentAgentLlmProfile() {
  return agentLlmProfileById(agentState.llmProfileId)
    || agentLlmProfileById(normalizeAgentLlmProfileId(agentState.llmProfileId));
}

function agentLlmProfileTitle(profile) {
  return String(profile?.title || profile?.model || profile?.id || 'Модель');
}

function agentLlmProfileMeta(profile) {
  return [profile?.endpointTitle || profile?.endpointId, profile?.model].filter(Boolean).join(' · ');
}

function setAgentModelDropdownOpen(open) {
  const dropdown = $('agent-model-dropdown');
  const button = $('agent-model-button');
  const menu = $('agent-model-menu');
  if (!dropdown || !button || !menu) return;
  const shouldOpen = Boolean(open) && !button.disabled && agentState.llmProfiles.length > 1;
  dropdown.classList.toggle('is-open', shouldOpen);
  menu.classList.toggle('hidden', !shouldOpen);
  button.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
  if (shouldOpen) setAgentChatListOpen(false);
}

function updateAgentModelDropdownSelection(profileId) {
  const profile = agentLlmProfileById(profileId);
  const label = $('agent-model-label');
  const button = $('agent-model-button');
  if (label) label.textContent = agentLlmProfileTitle(profile);
  if (button) {
    const meta = agentLlmProfileMeta(profile);
    button.title = [agentLlmProfileTitle(profile), meta].filter(Boolean).join('\n');
  }
  document.querySelectorAll('.agent-model-option').forEach((option) => {
    const active = option.dataset.profileId === String(profileId || '');
    option.classList.toggle('is-active', active);
    option.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function setAgentLlmProfile(profileId, options = {}) {
  const normalized = normalizeAgentLlmProfileId(profileId);
  agentState.llmProfileId = normalized;
  updateAgentModelDropdownSelection(normalized);
  if (!options.skipUsage) renderAgentUsage(agentState.usage);
  return normalized;
}

function setupAgentModelSelector() {
  const dropdown = $('agent-model-dropdown');
  const menu = $('agent-model-menu');
  if (!dropdown || !menu) return;
  const profiles = agentState.llmProfiles;
  menu.innerHTML = '';
  if (profiles.length <= 1) {
    dropdown.classList.add('hidden');
    setAgentModelDropdownOpen(false);
  } else {
    dropdown.classList.remove('hidden');
  }
  profiles.forEach((profile) => {
    const option = document.createElement('button');
    option.type = 'button';
    option.className = 'agent-model-option';
    option.dataset.profileId = String(profile.id || '');
    option.setAttribute('role', 'option');
    option.title = agentLlmProfileMeta(profile);
    option.innerHTML = `
      <span class="agent-model-option-title"></span>
      <span class="agent-model-option-meta"></span>
    `;
    option.querySelector('.agent-model-option-title').textContent = agentLlmProfileTitle(profile);
    option.querySelector('.agent-model-option-meta').textContent = agentLlmProfileMeta(profile);
    menu.appendChild(option);
  });
  setAgentLlmProfile(agentState.llmProfileId, { skipUsage: true });
}

async function changeAgentLlmProfile(profileId) {
  if (agentState.busy || agentState.loadingChat) return;
  const nextId = normalizeAgentLlmProfileId(profileId);
  const previousId = agentState.llmProfileId;
  if (!nextId || nextId === previousId) {
    setAgentLlmProfile(previousId, { skipUsage: true });
    setAgentModelDropdownOpen(false);
    return;
  }
  setAgentLlmProfile(nextId);
  setAgentModelDropdownOpen(false);
  updateAgentControls();
  try {
    if (agentState.chatId) {
      const payload = await patchAgentChatProfile(agentState.chatId, nextId);
      const chat = payload.chat;
      if (chat?.id) {
        agentState.chats = [
          chat,
          ...agentState.chats.filter((item) => item.id !== chat.id),
        ];
        renderAgentChatList();
      }
    }
  } catch (err) {
    setAgentLlmProfile(previousId);
    appendAgentNotice('error', err.message || 'Не удалось сменить модель');
  } finally {
    updateAgentControls();
  }
}

function getActivePage() {
  const path = normalizePath(window.location.pathname);
  if (path === `${CONSOLE_PATH}/analysis`) return 'analysis';
  if (path === CONSOLE_PATH) return 'analysis';
  return 'system';
}

function setupNavigation(activePage) {
  const systemHref = withToken(`${CONSOLE_PATH}/system`);
  const analysisHref = withToken(`${CONSOLE_PATH}/analysis`);
  const links = {
    system: $('nav-system'),
    analysis: $('nav-analysis'),
  };

  if (links.system) {
    links.system.href = systemHref;
    links.system.hidden = !IS_ADMIN;
  }
  if (links.analysis) links.analysis.href = analysisHref;

  for (const [page, link] of Object.entries(links)) {
    if (!link) continue;
    const isActive = page === activePage;
    link.classList.toggle('active', isActive);
    if (isActive) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }

  if (links.system && !links.system.dataset.analysisStateBound) {
    links.system.dataset.analysisStateBound = '1';
    links.system.addEventListener('click', () => saveAnalysisPageState());
  }
}

function showPage(activePage) {
  const pages = {
    system: $('system-page'),
    analysis: $('analysis-page'),
  };

  document.body.classList.toggle('page-system', activePage === 'system');
  document.body.classList.toggle('page-analysis', activePage === 'analysis');

  for (const [page, node] of Object.entries(pages)) {
    if (!node) continue;
    node.hidden = page !== activePage;
  }

  setupNavigation(activePage);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatValue(value) {
  if (value === true) return 'Истина';
  if (value === false) return 'Ложь';
  if (Array.isArray(value)) return value.map(formatValue).join(', ');
  if (value && typeof value === 'object') return JSON.stringify(value);
  return normalizeDisplayValue(String(value ?? ''));
}

function normalizeDisplayValue(value) {
  return String(value || '')
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part)
    .join(', ');
}

function compactRef(qn) {
  const parts = String(qn || '').split('/');
  if (parts.length >= 4) return `${parts[2]}.${parts.slice(3).join('.')}`;
  if (parts.length >= 2) return parts.slice(1).join(' / ');
  return qn || '';
}

function fullTreeTitle(...parts) {
  return parts.filter(Boolean).join(' / ');
}

function normalizeObjectTab(tab) {
  return tab === 'overview' ? 'summary' : (tab || 'summary');
}

function refPathParts(ref) {
  return String(ref || '').split('/').filter(Boolean);
}

function parentObjectRef(ref) {
  const parts = refPathParts(ref);
  if (parts.length <= 4) return ref;
  return parts.slice(0, 4).join('/');
}

function moduleSelectionKey(moduleId, ownerRef, moduleType) {
  if (String(moduleType || '') === 'CommonModule' && ownerRef) return `owner:${ownerRef}:CommonModule`;
  if (moduleId) return `id:${moduleId}`;
  if (ownerRef) return `owner:${ownerRef}:${moduleType || ''}`;
  return '';
}

function normalizeModuleRequest({ moduleId = '', ownerRef = '', moduleType = '' } = {}) {
  if (String(moduleType || '') === 'CommonModule' && ownerRef) {
    return { moduleId: '', ownerRef, moduleType: 'CommonModule' };
  }
  return { moduleId, ownerRef, moduleType };
}

function currentSelection() {
  if (analysisState.selectedKind === 'module') {
    const key = moduleSelectionKey(
      analysisState.selectedModuleId,
      analysisState.selectedModuleOwner,
      analysisState.selectedModuleType,
    );
    if (!key) return null;
    return {
      ref: key,
      type: 'module',
      moduleId: analysisState.selectedModuleId,
      ownerRef: analysisState.selectedModuleOwner,
      moduleType: analysisState.selectedModuleType,
      tab: analysisState.tab,
    };
  }
  if (!analysisState.selectedRef) return null;
  return {
    ref: analysisState.selectedRef,
    type: analysisState.selectedKind,
    section: analysisState.selectedSection,
    tab: analysisState.tab,
  };
}

function selectedOptionValues(values, options) {
  const allowed = new Set(options.map(([value]) => value));
  const selected = (Array.isArray(values) ? values : []).filter((value) => allowed.has(value));
  return selected.length ? selected : options.map(([value]) => value);
}

function treeBranchStateKey(branch) {
  if (!branch) return '';
  if (branch.classList.contains('tree-project')) return 'project';
  if (branch.classList.contains('tree-config')) return `config:${branch.dataset.config || ''}`;
  if (branch.classList.contains('tree-category-group')) {
    return `group:${branch.dataset.config || ''}:${branch.dataset.group || ''}`;
  }
  if (branch.classList.contains('tree-category')) {
    return `category:${branch.dataset.config || ''}:${branch.dataset.category || ''}`;
  }
  if (branch.classList.contains('tree-object-branch')) return `object:${branch.dataset.ref || ''}`;
  if (branch.classList.contains('tree-object-section-branch')) {
    return `section:${branch.dataset.ref || ''}:${branch.dataset.section || ''}`;
  }
  if (branch.classList.contains('tree-form-section-branch')) {
    return `form-section:${branch.dataset.formRef || ''}:${branch.dataset.section || ''}`;
  }
  if (branch.classList.contains('tree-form-branch')) return `form:${branch.dataset.formRef || branch.dataset.ref || ''}`;
  if (branch.classList.contains('tree-structure-branch')) return `structure:${branch.dataset.ref || ''}`;
  return '';
}

function openTreeBranchKeys() {
  return [...document.querySelectorAll('#analysis-tree .tree-branch.is-open')]
    .map(treeBranchStateKey)
    .filter(Boolean);
}

function analysisPageSnapshot() {
  const filter = $('analysis-filter');
  const treeEl = $('analysis-tree');
  const detailEl = $('analysis-detail');
  return {
    selectedKind: analysisState.selectedKind,
    selectedRef: analysisState.selectedRef,
    selectedConfig: analysisState.selectedConfig,
    selectedCategory: analysisState.selectedCategory,
    selectedSection: analysisState.selectedSection,
    selectedModuleId: analysisState.selectedModuleId,
    selectedModuleOwner: analysisState.selectedModuleOwner,
    selectedModuleType: analysisState.selectedModuleType,
    tab: analysisState.tab,
    backStack: analysisState.backStack.slice(-20),
    searchQuery: filter ? filter.value : analysisState.searchQuery,
    searchConfig: analysisState.searchConfig,
    searchTypes: analysisState.searchTypes,
    searchFields: analysisState.searchFields,
    searchVisibleCount: analysisState.searchVisibleCount,
    openBranches: openTreeBranchKeys(),
    treeScrollTop: treeEl ? treeEl.scrollTop : 0,
    detailScrollTop: detailEl ? detailEl.scrollTop : 0,
    savedAt: Date.now(),
  };
}

function saveAnalysisPageState() {
  if (getActivePage() !== 'analysis') return;
  try {
    sessionStorage.setItem(ANALYSIS_STATE_STORAGE_KEY, JSON.stringify(analysisPageSnapshot()));
  } catch {
    // State restore is a convenience feature; storage can be unavailable in private contexts.
  }
}

function readAnalysisPageState() {
  try {
    const raw = sessionStorage.getItem(ANALYSIS_STATE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

function savedSelectionFromUrl(params) {
  const ref = params.get('ref');
  if (!ref) return null;
  return {
    selectedKind: 'object',
    selectedRef: ref,
    selectedConfig: null,
    selectedCategory: null,
    selectedSection: null,
    tab: normalizeObjectTab(params.get('tab')),
    backStack: [],
    searchQuery: '',
    searchConfig: '',
    searchTypes: SEARCH_TYPE_OPTIONS.map(([value]) => value),
    searchFields: SEARCH_FIELD_OPTIONS.map(([value]) => value),
    openBranches: [],
    treeScrollTop: 0,
    detailScrollTop: 0,
  };
}

function applySavedSearchState(snapshot) {
  if (!snapshot) return '';
  analysisState.searchConfig = snapshot.searchConfig || '';
  analysisState.searchTypes = selectedOptionValues(snapshot.searchTypes, SEARCH_TYPE_OPTIONS);
  analysisState.searchFields = selectedOptionValues(snapshot.searchFields, SEARCH_FIELD_OPTIONS);
  analysisState.searchVisibleCount = Math.max(0, Number(snapshot.searchVisibleCount || 0));
  analysisState.pendingSearchVisibleCount = analysisState.searchVisibleCount;
  analysisState.searchQuery = String(snapshot.searchQuery || '').trim();
  const filter = $('analysis-filter');
  if (filter) filter.value = analysisState.searchQuery;
  updateSearchSettingsButton();
  return analysisState.searchQuery;
}

function restoreScrollPositions(snapshot) {
  if (!snapshot) return;
  window.setTimeout(() => {
    const treeEl = $('analysis-tree');
    const detailEl = $('analysis-detail');
    if (treeEl && Number.isFinite(Number(snapshot.treeScrollTop))) {
      treeEl.scrollTop = Number(snapshot.treeScrollTop);
    }
    if (detailEl && Number.isFinite(Number(snapshot.detailScrollTop))) {
      detailEl.scrollTop = Number(snapshot.detailScrollTop);
    }
  }, 0);
}

function findCategoryButton(config, category) {
  const branch = findCategoryBranch(config, category);
  return branch?.querySelector('.tree-node[data-kind="category"]') || null;
}

async function restoreOpenTreeBranches(keys = []) {
  const keySet = new Set(keys);
  if (!keySet.size) return;

  if (keySet.has('project')) {
    const project = document.querySelector('.tree-project');
    if (project) setTreeBranchOpen(project, true);
  }

  for (const branch of document.querySelectorAll('.tree-config, .tree-category-group')) {
    if (keySet.has(treeBranchStateKey(branch))) setTreeBranchOpen(branch, true);
  }

  const categoryKeys = [...keySet].filter((key) => key.startsWith('category:')).slice(0, 30);
  for (const key of categoryKeys) {
    const [, config, category] = key.split(':');
    const button = findCategoryButton(config, category);
    if (button) await selectCategory(button, { expand: true, renderDetail: false });
  }

  const objectKeys = [...keySet].filter((key) => key.startsWith('object:')).slice(0, 30);
  for (const key of objectKeys) {
    const ref = key.slice('object:'.length);
    const branch = findObjectBranch(ref);
    const container = branch?.querySelector(':scope > .tree-children');
    if (branch && container) {
      await renderObjectTreeChildren(ref, container);
      setTreeBranchOpen(branch, true);
    }
  }

  for (const branch of document.querySelectorAll('.tree-object-section-branch, .tree-structure-branch, .tree-form-section-branch')) {
    if (!keySet.has(treeBranchStateKey(branch))) continue;
    if (branch.classList.contains('tree-form-branch')) await renderFormTreeChildren(branch);
    setTreeBranchOpen(branch, true);
  }
}

async function restoreAnalysisSelection(snapshot) {
  if (!snapshot?.selectedKind) return false;
  analysisState.backStack = Array.isArray(snapshot.backStack) ? snapshot.backStack.slice(-20) : [];
  analysisState.tab = normalizeObjectTab(snapshot.tab);

  if (snapshot.selectedKind === 'project') {
    renderProjectDetail();
    return true;
  }
  if (snapshot.selectedKind === 'config' && snapshot.selectedConfig) {
    renderConfigDetail(snapshot.selectedConfig);
    return true;
  }
  if (snapshot.selectedKind === 'category-group' && snapshot.selectedConfig && snapshot.selectedCategory) {
    renderCategoryGroupDetail(snapshot.selectedConfig, snapshot.selectedCategory);
    return true;
  }
  if (snapshot.selectedKind === 'category' && snapshot.selectedConfig && snapshot.selectedCategory) {
    const button = findCategoryButton(snapshot.selectedConfig, snapshot.selectedCategory);
    if (button) {
      await selectCategory(button, { expand: true });
      return true;
    }
  }
  if (snapshot.selectedKind === 'object-section' && snapshot.selectedRef && snapshot.selectedSection) {
    await selectObjectSection(snapshot.selectedRef, snapshot.selectedSection, { pushBack: false });
    return true;
  }
  if (snapshot.selectedKind === 'node' && snapshot.selectedRef) {
    await selectNode(snapshot.selectedRef, '', { pushBack: false });
    return true;
  }
  if (snapshot.selectedKind === 'module' && (snapshot.selectedModuleId || snapshot.selectedModuleOwner)) {
    await selectModule({
      moduleId: snapshot.selectedModuleId,
      ownerRef: snapshot.selectedModuleOwner,
      moduleType: snapshot.selectedModuleType,
      pushBack: false,
    });
    return true;
  }
  if (snapshot.selectedRef) {
    await selectObject(snapshot.selectedRef, { pushBack: false, tab: snapshot.tab });
    return true;
  }
  return false;
}

async function restoreAnalysisStateAfterTreeLoad() {
  const snapshot = analysisState.pendingRestore;
  analysisState.pendingRestore = null;
  if (!snapshot) return false;

  const query = applySavedSearchState(snapshot);
  if (query) await runAnalysisSearch(query);
  else await restoreOpenTreeBranches(snapshot.openBranches || []);

  const restored = await restoreAnalysisSelection(snapshot);
  if (!restored && query) renderProjectDetail();
  restoreScrollPositions(snapshot);
  markActiveTreeNode();
  saveAnalysisPageState();
  return restored || Boolean(query);
}

function pushBackSelection(nextRef) {
  const current = currentSelection();
  if (!current || current.ref === nextRef) return;
  const last = analysisState.backStack[analysisState.backStack.length - 1];
  if (!last || last.ref !== current.ref) {
    analysisState.backStack.push(current);
  }
}

function clearNodeSelection(kind, config = null, category = null) {
  analysisState.selectedKind = kind;
  analysisState.selectedConfig = config;
  analysisState.selectedCategory = category;
  analysisState.selectedRef = null;
  analysisState.selectedObject = null;
  analysisState.selectedNode = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = null;
  analysisState.selectedModuleOwner = null;
  analysisState.selectedModuleType = null;
  analysisState.selectedSection = null;
  analysisState.relationships = null;
  analysisState.backStack = [];
  setAnalysisUrlRef('');
  markActiveTreeNode();
}

function clearGroupSelection(config, group) {
  analysisState.selectedKind = 'category-group';
  analysisState.selectedConfig = config;
  analysisState.selectedCategory = group;
  analysisState.selectedRef = null;
  analysisState.selectedObject = null;
  analysisState.selectedNode = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = null;
  analysisState.selectedModuleOwner = null;
  analysisState.selectedModuleType = null;
  analysisState.selectedSection = null;
  analysisState.relationships = null;
  analysisState.backStack = [];
  setAnalysisUrlRef('');
  markActiveTreeNode();
}

async function fetchJson(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(`${res.status}: ${data.error || data.message || res.statusText}`);
  }
  return data;
}

function showError(msg) {
  const box = $('error-box');
  box.textContent = msg;
  box.hidden = false;
}

function hideError() {
  $('error-box').hidden = true;
}

function showAnalysisError(msg) {
  const box = $('analysis-error');
  if (!box) return;
  box.textContent = msg;
  box.hidden = false;
}

function hideAnalysisError() {
  const box = $('analysis-error');
  if (box) box.hidden = true;
}

function setBadge(id, dotClass, text) {
  const badge = $(id);
  if (!badge) return;
  badge.querySelector('.dot').className = `dot ${dotClass}`;
  badge.querySelector('.label').textContent = text;
}

function renderBadges(health) {
  const container = $('badges');
  if (container.children.length === 0) {
    container.innerHTML = `
      <span class="badge" id="badge-neo4j"><span class="dot"></span><span class="label"></span></span>
      <span class="badge" id="badge-transport"><span class="label"></span></span>
      <span class="badge" id="badge-mcp"><span class="label"></span></span>
      <span class="badge" id="badge-version"><span class="label"></span></span>
    `;
  }
  const dot = health.neo4j_connected ? 'green' : 'red';
  const label = health.neo4j_connected ? 'Neo4j connected' : 'Neo4j disconnected';
  setBadge('badge-neo4j', dot, label);
  $('badge-transport').querySelector('.label').textContent = `Transport: ${health.mcp_transport || '-'}`;
  $('badge-mcp').querySelector('.label').textContent = `MCP: ${health.mcp_path || '-'}`;
  $('badge-version').querySelector('.label').textContent = `Version: ${APP_VERSION || '-'}`;
}

function formatRuNumber(value, options = {}) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return number.toLocaleString('ru-RU', options);
}

function formatRuDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).replace(',', '');
}

function renderStats(data) {
  const stats = data.stats || {};
  const grid = $('stats-grid');
  grid.innerHTML = '';

  for (const [key, value] of Object.entries(stats)) {
    if (value === 0 && !PINNED.has(key)) continue;
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML =
      `<div class="card-label">${escapeHtml(LABELS[key] || key)}</div>` +
      `<div class="card-value">${formatRuNumber(value)}</div>`;
    grid.appendChild(card);
  }
}

function formatRuntimeNumber(value) {
  if (value === null || value === undefined || value === '') return '—';
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return formatRuNumber(Math.round(number));
}

function formatRuntimeDuration(ms) {
  const value = Number(ms || 0);
  if (!Number.isFinite(value) || value <= 0) return '—';
  if (value < 1000) return `${formatRuNumber(Math.round(value))} мс`;
  const seconds = value / 1000;
  if (seconds < 60) return `${formatRuNumber(seconds, { maximumFractionDigits: 1 })} с`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${formatRuNumber(minutes, { maximumFractionDigits: 1 })} мин`;
  const hours = minutes / 60;
  return `${formatRuNumber(hours, { maximumFractionDigits: 1 })} ч`;
}

function formatRuntimeCosts(costs) {
  if (!Array.isArray(costs) || !costs.length) return '—';
  const groups = new Map();
  for (const cost of costs) {
    const amount = Number(cost.amount);
    if (!Number.isFinite(amount)) continue;
    const unit = cost.unit || '';
    if (!groups.has(unit)) groups.set(unit, []);
    groups.get(unit).push({ ...cost, amount });
  }
  const parts = [];
  for (const [unit, items] of groups.entries()) {
    const total = items.reduce((sum, item) => sum + item.amount, 0);
    const totalText = formatRuNumber(total, { maximumFractionDigits: 8 });
    parts.push(`${totalText}${unit ? ` ${escapeHtml(unit)}` : ''}`);
  }
  return parts.join(' · ') || '—';
}

function formatRuntimeModels(models) {
  if (!Array.isArray(models) || !models.length) return '—';
  const first = models.slice(0, 2).map((item) => {
    const provider = item.provider && item.provider !== 'unknown' ? `${item.provider}/` : '';
    return `${provider}${item.model || 'unknown'}`;
  });
  const suffix = models.length > 2 ? ` +${models.length - 2}` : '';
  return `${first.join(' · ')}${suffix}`;
}

function formatRuntimeTokens(item) {
  const input = item?.input_tokens;
  const output = item?.output_tokens;
  const total = item?.total_tokens;
  if (input == null && output == null && total == null) return '—';
  return `in ${formatRuntimeNumber(input)} · out ${formatRuntimeNumber(output)} · total ${formatRuntimeNumber(total)}`;
}

function formatRuntimeRowTokens(item) {
  const input = item?.input_tokens;
  const output = item?.output_tokens;
  if (input == null && output == null) return '—';
  return `in ${formatRuntimeNumber(input)} · out ${formatRuntimeNumber(output)}`;
}

function runtimeUsageHasRows(data) {
  return (data?.sections || []).some((section) => Array.isArray(section.items) && section.items.length);
}

function setupRuntimeUsageEvents() {
  const section = $('runtime-usage-section');
  if (!section || section.dataset.bound) return;
  section.dataset.bound = '1';
  section.addEventListener('click', (event) => {
    const button = event.target.closest('[data-runtime-scope]');
    if (!button) return;
    const scope = button.dataset.runtimeScope || 'all';
    if (scope === runtimeUsageState.scope) return;
    runtimeUsageState.scope = scope;
    loadRuntimeUsage(scope);
  });
}

async function loadRuntimeUsage(scope = runtimeUsageState.scope) {
  const section = $('runtime-usage-section');
  if (!section) return;
  try {
    const response = await fetch(withToken(`${API_PREFIX}/runtime/usage?scope=${encodeURIComponent(scope)}`));
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      renderRuntimeUsageError(`Статистика ${response.status}: ${err.error || err.message || response.statusText}`);
      return;
    }
    renderRuntimeUsageSection(await response.json());
  } catch (err) {
    renderRuntimeUsageError(`Статистика: ${err.message || err}`);
  }
}

function renderRuntimeUsageSection(data) {
  const section = $('runtime-usage-section');
  if (!section) return;
  setupRuntimeUsageEvents();
  runtimeUsageState.data = data || null;
  const scope = data?.scope || runtimeUsageState.scope || 'all';
  runtimeUsageState.scope = scope;
  const scopeButtons = `
    <div class="runtime-usage-scope" role="group" aria-label="Период статистики">
      <button type="button" data-runtime-scope="all" class="${scope === 'all' ? 'active' : ''}">Все запуски</button>
      <button type="button" data-runtime-scope="current" class="${scope === 'current' ? 'active' : ''}">Текущий запуск</button>
    </div>
  `;
  const empty = !runtimeUsageHasRows(data);
  section.innerHTML = `
    <div class="runtime-usage-header">
      <h2 id="runtime-usage-title">Статистика использования</h2>
      ${scopeButtons}
    </div>
    ${empty ? `<div class="runtime-usage-empty">${data?.available === false ? 'Статистика пока не записывалась.' : 'Данных пока нет.'}</div>` : ''}
    <div class="runtime-usage-groups">
      ${empty ? '' : (data?.sections || []).map(renderRuntimeUsageGroup).join('')}
    </div>
  `;
}

function renderRuntimeUsageError(message) {
  const section = $('runtime-usage-section');
  if (!section) return;
  section.innerHTML = `
    <div class="runtime-usage-header">
      <h2 id="runtime-usage-title">Статистика использования</h2>
    </div>
    <div class="runtime-usage-error">${escapeHtml(message)}</div>
  `;
}

function renderRuntimeUsageGroup(section) {
  const items = Array.isArray(section.items) ? section.items : [];
  const totals = section.totals || {};
  return `
    <section class="runtime-usage-group">
      <div class="runtime-usage-group-header">
        <h3>${escapeHtml(section.title || section.key || '')}</h3>
        <span>${formatRuntimeNumber(totals.calls)} вызовов</span>
      </div>
      <div class="runtime-usage-total-row">
        <span>Ошибки: ${formatRuntimeNumber(totals.failures)}</span>
        <span>Токены: ${formatRuntimeTokens(totals)}</span>
        <span>Стоимость: ${formatRuntimeCosts(totals.costs)}</span>
        <span>Время: ${formatRuntimeDuration(totals.duration_ms_total)}</span>
      </div>
      ${items.length ? `
        <div class="runtime-usage-list">
          ${items.map(renderRuntimeUsageItem).join('')}
        </div>
      ` : '<div class="runtime-usage-group-empty">Нет данных</div>'}
    </section>
  `;
}

function renderRuntimeUsageItem(item) {
  const failures = Number(item.failures || 0);
  const statusClass = failures > 0 ? 'has-failures' : '';
  const children = Array.isArray(item.children) ? item.children : [];
  const modelRows = children.length > 1 ? children : [];
  return `
    <div class="runtime-usage-item ${statusClass}">
      <div class="runtime-usage-item-main">
        <span class="runtime-usage-label">${escapeHtml(item.label || item.event_type || '')}</span>
        <span class="runtime-usage-event">${escapeHtml(item.event_type || '')}</span>
      </div>
      <div class="runtime-usage-item-metrics">
        <span><b>${formatRuntimeNumber(item.calls)} вызовов</b><small>${formatRuntimeNumber(item.successes)} ok · ${formatRuntimeNumber(item.failures)} err</small></span>
        <span><b>${formatRuntimeRowTokens(item)}</b><small>токены</small></span>
        <span><b>${formatRuntimeCosts(item.costs)}</b><small>стоимость</small></span>
        <span><b>${formatRuntimeDuration(item.avg_duration_ms)}</b><small>среднее</small></span>
      </div>
      ${modelRows.length ? renderRuntimeUsageChildren(modelRows) : `<div class="runtime-usage-models">${escapeHtml(formatRuntimeModels(item.models))}</div>`}
    </div>
  `;
}

function renderRuntimeUsageChildren(children) {
  return `
    <div class="runtime-usage-children">
      <div class="runtime-usage-children-title">По моделям</div>
      ${children.map(renderRuntimeUsageChild).join('')}
    </div>
  `;
}

function renderRuntimeUsageChild(item) {
  const provider = item.provider && item.provider !== 'unknown' ? item.provider : '';
  const model = item.model || 'unknown';
  return `
    <div class="runtime-usage-child">
      <div class="runtime-usage-child-main">
        <span>${escapeHtml(model)}</span>
        ${provider ? `<small>${escapeHtml(provider)}</small>` : ''}
      </div>
      <div class="runtime-usage-child-metrics">
        <span><b>${formatRuntimeNumber(item.calls)} вызовов</b><small>${formatRuntimeNumber(item.successes)} ok · ${formatRuntimeNumber(item.failures)} err</small></span>
        <span><b>${formatRuntimeRowTokens(item)}</b><small>токены</small></span>
        <span><b>${formatRuntimeCosts(item.costs)}</b><small>стоимость</small></span>
        <span><b>${formatRuntimeDuration(item.avg_duration_ms)}</b><small>среднее</small></span>
      </div>
    </div>
  `;
}

function prettyJson(value) {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value ?? '');
  }
}

function mcpToolParamCount(tool) {
  const count = Number(tool?.parameter_count);
  if (Number.isFinite(count)) return count;
  const props = tool?.parameters?.properties;
  return props && typeof props === 'object' ? Object.keys(props).length : 0;
}

function shortToolDescription(tool) {
  return String(tool?.description || '')
    .split('\n')
    .map((part) => part.trim())
    .filter(Boolean)[0] || 'Без описания';
}

function setupMcpToolsEvents() {
  const section = $('mcp-tools-section');
  if (!section || section.dataset.bound) return;
  section.dataset.bound = '1';
  section.addEventListener('click', (event) => {
    const card = event.target.closest('[data-role="mcp-tool-open"]');
    if (!card) return;
    event.preventDefault();
    showMcpToolModal(card.dataset.toolName || '');
  });
  section.addEventListener('change', (event) => {
    const toggle = event.target.closest('[data-role="mcp-tool-toggle"]');
    if (!toggle) return;
    handleMcpToolToggle(toggle);
  });
}

function renderMcpToolsSection(data) {
  const section = $('mcp-tools-section');
  if (!section) return;
  setupMcpToolsEvents();
  mcpToolsState.tools = Array.isArray(data?.tools) ? data.tools : [];
  const total = Number(data?.count ?? mcpToolsState.tools.length) || mcpToolsState.tools.length;
  const enabled = Number(data?.enabled_count ?? mcpToolsState.tools.filter((tool) => tool.enabled !== false).length) || 0;

  section.innerHTML = `
    <div class="mcp-tools-header">
      <div>
        <h2 id="mcp-tools-title">MCP инструменты</h2>
      </div>
      <div class="mcp-tools-count">${formatRuNumber(enabled)} / ${formatRuNumber(total)} включено</div>
    </div>
    <div id="mcp-tools-list" class="mcp-tools-list"></div>
  `;
  renderMcpToolsList();
}

function renderMcpToolsError(message) {
  const section = $('mcp-tools-section');
  if (!section) return;
  section.innerHTML = `
    <div class="mcp-tools-header">
      <div>
        <h2 id="mcp-tools-title">MCP инструменты</h2>
        <p class="subtitle">Не удалось загрузить список tools.</p>
      </div>
    </div>
    <div class="mcp-tools-error">${escapeHtml(message)}</div>
  `;
}

function renderMcpToolsList() {
  const list = $('mcp-tools-list');
  if (!list) return;
  const tools = mcpToolsState.tools;
  const countEl = $('mcp-tools-section')?.querySelector('.mcp-tools-count');
  if (countEl) {
    const enabled = tools.filter((tool) => tool.enabled !== false).length;
    countEl.textContent = `${formatRuNumber(enabled)} / ${formatRuNumber(tools.length)} включено`;
  }
  if (!tools.length) {
    list.innerHTML = '<div class="mcp-tools-empty">Tools не найдены</div>';
    return;
  }

  list.innerHTML = tools.map((tool) => {
    const paramCount = mcpToolParamCount(tool);
    const enabled = tool.enabled !== false;
    const title = enabled ? 'Выключить MCP tool' : 'Включить MCP tool';
    return `
      <div class="mcp-tool-card ${enabled ? '' : 'is-disabled'}" data-tool-name="${escapeHtml(tool.name)}">
        <button class="mcp-tool-open" type="button" data-role="mcp-tool-open" data-tool-name="${escapeHtml(tool.name)}">
          <span class="mcp-tool-name">${escapeHtml(tool.name)}</span>
          <span class="mcp-tool-param-count">${formatRuNumber(paramCount)} параметров</span>
        </button>
        <label class="mcp-tool-switch" title="${title}">
          <input type="checkbox" data-role="mcp-tool-toggle" data-tool-name="${escapeHtml(tool.name)}"
                 ${enabled ? 'checked' : ''} aria-label="${title}">
          <span aria-hidden="true"></span>
        </label>
      </div>
    `;
  }).join('');
}

async function handleMcpToolToggle(input) {
  const name = input.dataset.toolName || '';
  const tool = getMcpToolByName(name);
  if (!tool) return;
  const previous = tool.enabled !== false;
  const next = Boolean(input.checked);
  const card = input.closest('.mcp-tool-card');
  input.disabled = true;
  card?.classList.add('is-pending');
  try {
    const res = await fetch(withToken(`${API_PREFIX}/mcp/tools/${encodeURIComponent(name)}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(payload.message || payload.error || res.statusText);
    }
    tool.enabled = Boolean(payload.enabled);
  } catch (err) {
    tool.enabled = previous;
    showError(`MCP tool ${name}: ${err.message || err}`);
  } finally {
    renderMcpToolsList();
  }
}

function renderConsoleUsersSection(payload) {
  const section = $('console-users-section');
  if (!section) return;
  const users = Array.isArray(payload?.users) ? payload.users : [];
  consoleUsersState.users = users;
  section.innerHTML = `
    <div class="console-users-header">
      <div>
        <h2 id="console-users-title">Пользователи</h2>
        <span class="console-users-count">${formatRuNumber(users.length)} пользователей</span>
      </div>
      <button class="secondary-button" type="button" data-role="console-user-add">Добавить</button>
    </div>
    ${users.length ? `
      <div class="console-users-list">
        ${users.map(renderConsoleUserRow).join('')}
      </div>
    ` : '<div class="console-users-empty">Пользователи не созданы</div>'}
  `;
  if (!section.dataset.bound) {
    section.dataset.bound = '1';
    section.addEventListener('click', handleConsoleUsersClick);
    section.addEventListener('change', handleConsoleUsersChange);
  }
}

function renderConsoleUsersError(message) {
  const section = $('console-users-section');
  if (!section) return;
  section.innerHTML = `
    <div class="console-users-header">
      <h2 id="console-users-title">Пользователи</h2>
    </div>
    <div class="error-box">${escapeHtml(message)}</div>
  `;
}

function renderConsoleUserRow(user) {
  const enabled = user.enabled !== false;
  const lastSeen = formatRuDateTime(user.last_seen_at);
  const isEnvAdmin = user.role === 'admin' && user.source === 'env_admin';
  const tokenAction = isEnvAdmin
    ? '<span class="console-user-token-source" title="Токен задается через WEB_CONSOLE_ADMIN_TOKEN">Токен из env</span>'
    : '<button class="secondary-button" type="button" data-role="console-user-rotate">Сбросить токен</button>';
  return `
    <div class="console-user-card ${enabled ? '' : 'is-disabled'}" data-user-id="${escapeHtml(user.id)}">
      <div class="console-user-main">
        <strong>${escapeHtml(user.login || '')}</strong>
        <span>${escapeHtml(user.source || '')}</span>
      </div>
      <label>
        <span>Имя</span>
        <input type="text" data-role="console-user-display-name" value="${escapeHtml(user.display_name || '')}">
      </label>
      <label>
        <span>Роль</span>
        <select data-role="console-user-role">
          <option value="user" ${user.role === 'user' ? 'selected' : ''}>user</option>
          <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>admin</option>
        </select>
      </label>
      <label class="console-user-enabled">
        <input type="checkbox" data-role="console-user-enabled" ${enabled ? 'checked' : ''}>
        <span>Включен</span>
      </label>
      <div class="console-user-last-seen">
        <span>Последний вход</span>
        <strong>${escapeHtml(lastSeen)}</strong>
      </div>
      ${tokenAction}
    </div>
  `;
}

function getConsoleUserById(userId) {
  return consoleUsersState.users.find((user) => user.id === userId) || null;
}

async function handleConsoleUsersClick(event) {
  const addButton = event.target.closest('[data-role="console-user-add"]');
  if (addButton) {
    showCreateConsoleUserModal();
    return;
  }
  const rotateButton = event.target.closest('[data-role="console-user-rotate"]');
  if (rotateButton) {
    await rotateConsoleUserToken(rotateButton);
  }
}

async function handleConsoleUsersChange(event) {
  const field = event.target.closest('[data-role^="console-user-"]');
  if (!field || field.dataset.role === 'console-user-rotate') return;
  const card = field.closest('.console-user-card');
  const userId = card?.dataset.userId || '';
  const user = getConsoleUserById(userId);
  if (!user) return;
  const payload = {
    display_name: card.querySelector('[data-role="console-user-display-name"]')?.value || '',
    role: card.querySelector('[data-role="console-user-role"]')?.value || user.role,
    enabled: Boolean(card.querySelector('[data-role="console-user-enabled"]')?.checked),
  };
  await patchConsoleUser(userId, payload);
}

async function patchConsoleUser(userId, payload) {
  try {
    const res = await fetch(withToken(`${API_PREFIX}/users/${encodeURIComponent(userId)}`), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || res.statusText);
    const index = consoleUsersState.users.findIndex((user) => user.id === userId);
    if (index >= 0) consoleUsersState.users[index] = data.user;
    renderConsoleUsersSection({ users: consoleUsersState.users });
  } catch (err) {
    showError(`Пользователь: ${err.message || err}`);
    await reloadConsoleUsers();
  }
}

async function rotateConsoleUserToken(button) {
  const card = button.closest('.console-user-card');
  const userId = card?.dataset.userId || '';
  const user = getConsoleUserById(userId);
  if (!user) return;
  button.disabled = true;
  try {
    const res = await fetch(withToken(`${API_PREFIX}/users/${encodeURIComponent(userId)}/rotate-token`), {
      method: 'POST',
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || res.statusText);
    const index = consoleUsersState.users.findIndex((item) => item.id === userId);
    if (index >= 0) consoleUsersState.users[index] = data.user;
    renderConsoleUsersSection({ users: consoleUsersState.users });
    showConsoleUserTokenModal(data.user || user, data.token || '');
  } catch (err) {
    showError(`Сброс токена: ${err.message || err}`);
  } finally {
    button.disabled = false;
  }
}

async function reloadConsoleUsers() {
  try {
    const res = await fetch(withToken(`${API_PREFIX}/users`));
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || res.statusText);
    renderConsoleUsersSection(data);
  } catch (err) {
    renderConsoleUsersError(`Пользователи: ${err.message || err}`);
  }
}

function showCreateConsoleUserModal() {
  const modal = ensureCreateConsoleUserModal();
  modal.hidden = false;
  const form = modal.querySelector('[data-role="console-user-form"]');
  form?.reset();
  form?.querySelector('[name="role"]') && (form.querySelector('[name="role"]').value = 'user');
  form?.querySelector('[name="enabled"]') && (form.querySelector('[name="enabled"]').checked = true);
}

function closeCreateConsoleUserModal() {
  const modal = $('console-user-modal');
  if (modal) modal.hidden = true;
}

function ensureCreateConsoleUserModal() {
  let modal = $('console-user-modal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'console-user-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="console-user-close"></div>
    <section class="condition-modal-dialog console-user-dialog" role="dialog" aria-modal="true"
             aria-labelledby="console-user-modal-title">
      <header class="condition-modal-header">
        <h3 id="console-user-modal-title">Новый пользователь</h3>
        <button class="panel-icon-button" type="button" data-role="console-user-close"
                title="Закрыть" aria-label="Закрыть">×</button>
      </header>
      <form class="console-user-form" data-role="console-user-form">
        <label>
          <span>Логин</span>
          <input name="login" type="text" required autocomplete="off">
        </label>
        <label>
          <span>Имя</span>
          <input name="display_name" type="text" autocomplete="off">
        </label>
        <label>
          <span>Роль</span>
          <select name="role">
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label class="console-user-enabled">
          <input name="enabled" type="checkbox" checked>
          <span>Включен</span>
        </label>
        <footer class="condition-modal-footer">
          <button class="secondary-button" type="button" data-role="console-user-close">Отмена</button>
          <button class="primary-button" type="submit">Создать</button>
        </footer>
      </form>
    </section>
  `;
  modal.addEventListener('click', (event) => {
    if (event.target.closest('[data-role="console-user-close"]')) closeCreateConsoleUserModal();
  });
  modal.querySelector('[data-role="console-user-form"]').addEventListener('submit', createConsoleUserFromModal);
  document.body.appendChild(modal);
  return modal;
}

async function createConsoleUserFromModal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  const payload = {
    login: form.elements.login.value.trim(),
    display_name: form.elements.display_name.value.trim(),
    role: form.elements.role.value,
    enabled: Boolean(form.elements.enabled.checked),
  };
  try {
    const res = await fetch(withToken(`${API_PREFIX}/users`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || data.error || res.statusText);
    closeCreateConsoleUserModal();
    await reloadConsoleUsers();
    showConsoleUserTokenModal(data.user || payload, data.token || '');
  } catch (err) {
    showError(`Создание пользователя: ${err.message || err}`);
  } finally {
    submit.disabled = false;
  }
}

function consoleUserTokenParam(user) {
  return user?.role === 'admin' ? 'admin_token' : 'user_token';
}

function consoleUserEntryPath(user) {
  return user?.role === 'admin' ? `${CONSOLE_PATH}/system` : `${CONSOLE_PATH}/analysis`;
}

function buildConsoleUserAccessUrl(user, token) {
  const url = new URL(consoleUserEntryPath(user), window.location.origin);
  url.searchParams.set(consoleUserTokenParam(user), token);
  return url.toString();
}

function showConsoleUserTokenModal(user, token) {
  const modal = ensureConsoleUserTokenModal();
  const login = typeof user === 'string' ? user : (user?.login || '');
  const safeUser = typeof user === 'string' ? { login, role: 'user' } : (user || {});
  const accessUrl = buildConsoleUserAccessUrl(safeUser, token);
  consoleUsersState.tokenToShow = token;
  consoleUsersState.urlToShow = accessUrl;
  consoleUsersState.tokenLogin = login;
  modal.querySelector('[data-role="console-user-token-login"]').textContent = login;
  modal.querySelector('[data-role="console-user-token-value"]').textContent = token;
  modal.querySelector('[data-role="console-user-token-url"]').textContent = accessUrl;
  modal.hidden = false;
}

function closeConsoleUserTokenModal() {
  const modal = $('console-user-token-modal');
  if (modal) modal.hidden = true;
}

function ensureConsoleUserTokenModal() {
  let modal = $('console-user-token-modal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'console-user-token-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="console-user-token-close"></div>
    <section class="condition-modal-dialog console-user-dialog" role="dialog" aria-modal="true">
      <header class="condition-modal-header">
        <div>
          <h3>Токен пользователя</h3>
          <span class="muted" data-role="console-user-token-login"></span>
        </div>
        <button class="panel-icon-button" type="button" data-role="console-user-token-close"
                title="Закрыть" aria-label="Закрыть">×</button>
      </header>
      <div class="console-user-token-body">
        <p class="muted">Скопируйте токен сейчас. После закрытия он больше не будет показан.</p>
        <label class="console-user-token-field">
          <span>Токен</span>
          <div class="console-user-token-box" data-role="console-user-token-value"></div>
        </label>
        <label class="console-user-token-field">
          <span>URL для входа</span>
          <div class="console-user-token-box" data-role="console-user-token-url"></div>
        </label>
      </div>
      <footer class="condition-modal-footer console-user-token-footer">
        <button class="secondary-button" type="button" data-role="console-user-token-copy-url">Скопировать URL</button>
        <button class="secondary-button" type="button" data-role="console-user-token-copy">Скопировать токен</button>
        <button class="primary-button" type="button" data-role="console-user-token-close">Готово</button>
      </footer>
    </section>
  `;
  modal.addEventListener('click', async (event) => {
    if (event.target.closest('[data-role="console-user-token-close"]')) closeConsoleUserTokenModal();
    const copyButton = event.target.closest('[data-role="console-user-token-copy"]');
    if (copyButton) {
      await copyTextToClipboard(consoleUsersState.tokenToShow || '');
      copyButton.textContent = 'Скопировано';
      window.setTimeout(() => {
        copyButton.textContent = 'Скопировать токен';
      }, 1400);
    }
    const copyUrlButton = event.target.closest('[data-role="console-user-token-copy-url"]');
    if (copyUrlButton) {
      await copyTextToClipboard(consoleUsersState.urlToShow || '');
      copyUrlButton.textContent = 'Скопировано';
      window.setTimeout(() => {
        copyUrlButton.textContent = 'Скопировать URL';
      }, 1400);
    }
  });
  document.body.appendChild(modal);
  return modal;
}

function getMcpToolByName(name) {
  return mcpToolsState.tools.find((tool) => tool.name === name) || null;
}

function ensureMcpToolModal() {
  let modal = $('mcp-tool-modal');
  if (modal) return modal;

  modal = document.createElement('div');
  modal.id = 'mcp-tool-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="mcp-tool-close"></div>
    <section class="condition-modal-dialog mcp-tool-dialog" role="dialog" aria-modal="true"
             aria-labelledby="mcp-tool-modal-title">
      <header class="condition-modal-header mcp-tool-window-header" data-role="mcp-tool-drag">
        <div>
          <div class="mcp-tool-title-row">
            <h3 id="mcp-tool-modal-title"></h3>
            <span data-role="mcp-tool-modal-meta"></span>
          </div>
        </div>
        <div class="mcp-tool-window-actions">
          <button class="panel-icon-button" type="button" data-role="mcp-tool-fullscreen"
                  title="Развернуть" aria-label="Развернуть">⛶</button>
          <button class="panel-icon-button" type="button" data-role="mcp-tool-close"
                  title="Закрыть" aria-label="Закрыть">×</button>
        </div>
      </header>
      <div class="mcp-tool-modal-body">
        <div class="tabs mcp-tool-tabs" data-role="mcp-tool-tabs"></div>
        <div class="mcp-tool-tab-body" data-role="mcp-tool-tab-body"></div>
      </div>
    </section>
  `;
  modal.addEventListener('click', (event) => {
    if (event.target.closest('[data-role="mcp-tool-close"]')) closeMcpToolModal();
    if (event.target.closest('[data-role="mcp-tool-fullscreen"]')) toggleMcpToolModalFullscreen();
    const tab = event.target.closest('[data-role="mcp-tool-tab"]');
    if (tab) {
      mcpToolsState.activeTab = tab.dataset.tab || 'description';
      renderMcpToolModalBody();
    }
    const copyButton = event.target.closest('[data-role="mcp-json-copy"]');
    if (copyButton) handleMcpJsonCopy(copyButton);
  });
  modal.addEventListener('pointerdown', startMcpToolModalDrag);
  modal.addEventListener('pointerup', rememberMcpToolModalFrame);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) closeMcpToolModal();
  });
  document.addEventListener('pointermove', moveMcpToolModalDrag);
  document.addEventListener('pointerup', stopMcpToolModalDrag);
  document.body.appendChild(modal);
  return modal;
}

function showMcpToolModal(name) {
  const tool = getMcpToolByName(name);
  if (!tool) return;
  const modal = ensureMcpToolModal();
  mcpToolsState.activeToolName = name;
  mcpToolsState.activeTab = 'description';
  modal.hidden = false;
  positionMcpToolModal();
  renderMcpToolModalBody();
}

function closeMcpToolModal() {
  const modal = $('mcp-tool-modal');
  if (modal) modal.hidden = true;
  mcpToolsState.drag = null;
}

function getMcpToolDialog() {
  return $('mcp-tool-modal')?.querySelector('.mcp-tool-dialog') || null;
}

function positionMcpToolModal() {
  const dialog = getMcpToolDialog();
  if (!dialog || mcpToolsState.modalFullscreen) return;
  const frame = mcpToolsState.modalFrame;
  if (frame) {
    dialog.style.left = `${frame.left}px`;
    dialog.style.top = `${frame.top}px`;
    dialog.style.width = `${frame.width}px`;
    dialog.style.height = `${frame.height}px`;
    return;
  }
  const width = Math.min(1120, Math.round(window.innerWidth * 0.92));
  const height = Math.min(820, Math.round(window.innerHeight * 0.86));
  const left = Math.max(12, Math.round((window.innerWidth - width) / 2));
  const top = Math.max(12, Math.round((window.innerHeight - height) / 2));
  mcpToolsState.modalFrame = { left, top, width, height };
  dialog.style.left = `${left}px`;
  dialog.style.top = `${top}px`;
  dialog.style.width = `${width}px`;
  dialog.style.height = `${height}px`;
}

function rememberMcpToolModalFrame() {
  const dialog = getMcpToolDialog();
  if (!dialog || mcpToolsState.modalFullscreen) return;
  const rect = dialog.getBoundingClientRect();
  mcpToolsState.modalFrame = {
    left: Math.round(rect.left),
    top: Math.round(rect.top),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}

function toggleMcpToolModalFullscreen() {
  const modal = $('mcp-tool-modal');
  const dialog = getMcpToolDialog();
  if (!modal || !dialog) return;
  if (!mcpToolsState.modalFullscreen) rememberMcpToolModalFrame();
  mcpToolsState.modalFullscreen = !mcpToolsState.modalFullscreen;
  dialog.classList.toggle('is-fullscreen', mcpToolsState.modalFullscreen);
  const button = modal.querySelector('[data-role="mcp-tool-fullscreen"]');
  if (button) {
    button.textContent = mcpToolsState.modalFullscreen ? '⤡' : '⛶';
    button.title = mcpToolsState.modalFullscreen ? 'Вернуть размер' : 'Развернуть';
    button.setAttribute('aria-label', button.title);
  }
  if (!mcpToolsState.modalFullscreen) positionMcpToolModal();
}

function startMcpToolModalDrag(event) {
  const handle = event.target.closest('[data-role="mcp-tool-drag"]');
  if (!handle || event.target.closest('button') || mcpToolsState.modalFullscreen) return;
  const dialog = getMcpToolDialog();
  if (!dialog) return;
  const rect = dialog.getBoundingClientRect();
  mcpToolsState.drag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  };
  dialog.setPointerCapture?.(event.pointerId);
  event.preventDefault();
}

function moveMcpToolModalDrag(event) {
  const drag = mcpToolsState.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const dialog = getMcpToolDialog();
  if (!dialog) return;
  const maxLeft = Math.max(8, window.innerWidth - drag.width - 8);
  const maxTop = Math.max(8, window.innerHeight - 80);
  const left = Math.min(maxLeft, Math.max(8, drag.left + event.clientX - drag.startX));
  const top = Math.min(maxTop, Math.max(8, drag.top + event.clientY - drag.startY));
  dialog.style.left = `${Math.round(left)}px`;
  dialog.style.top = `${Math.round(top)}px`;
}

function stopMcpToolModalDrag(event) {
  const drag = mcpToolsState.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  rememberMcpToolModalFrame();
  mcpToolsState.drag = null;
}

function renderMcpToolModalBody() {
  const modal = $('mcp-tool-modal');
  const tool = getMcpToolByName(mcpToolsState.activeToolName);
  if (!modal || !tool) return;

  modal.querySelector('#mcp-tool-modal-title').textContent = tool.name || 'MCP tool';
  modal.querySelector('[data-role="mcp-tool-modal-meta"]').textContent =
    `${formatRuNumber(mcpToolParamCount(tool))} параметров`;

  const tabs = [
    ['description', 'Описание'],
    ['parameters', 'Параметры'],
    ['returns', 'Что возвращает'],
    ['examples', 'Примеры ответов'],
  ];
  const tabsEl = modal.querySelector('[data-role="mcp-tool-tabs"]');
  tabsEl.innerHTML = tabs.map(([key, label]) => `
    <button type="button" class="tab-button ${mcpToolsState.activeTab === key ? 'active' : ''}"
            data-role="mcp-tool-tab" data-tab="${key}">
      ${escapeHtml(label)}
    </button>
  `).join('');

  const body = modal.querySelector('[data-role="mcp-tool-tab-body"]');
  if (mcpToolsState.activeTab === 'parameters') {
    body.innerHTML = renderMcpParametersTab(tool);
  } else if (mcpToolsState.activeTab === 'returns') {
    body.innerHTML = renderMcpReturnsTab(tool);
  } else if (mcpToolsState.activeTab === 'examples') {
    body.innerHTML = renderMcpExamplesTab(tool);
  } else {
    body.innerHTML = `
      <div class="mcp-tool-tab-fill mcp-tool-description-tab">
        <div class="mcp-tool-description-full">${escapeHtml(tool.description || 'Без описания')}</div>
      </div>
    `;
  }
}

function renderMcpParametersTab(tool) {
  return `
    <div class="mcp-tool-tab-fill mcp-tool-params-tab">
      ${renderMcpParameterTable(tool.parameters || {})}
      <details class="mcp-schema-details">
        <summary>Raw schema</summary>
        ${renderMcpJsonBlock(tool.parameters || {}, 'parameters.json')}
      </details>
    </div>
  `;
}

function renderMcpParameterTable(schema) {
  const props = schema && typeof schema === 'object' && schema.properties && typeof schema.properties === 'object'
    ? schema.properties
    : {};
  const required = new Set(Array.isArray(schema?.required) ? schema.required : []);
  const entries = Object.entries(props);
  if (!entries.length) {
    return '<div class="mcp-tools-empty">У этого tool нет параметров.</div>';
  }
  return `
    <div class="mcp-return-fields mcp-parameter-fields">
      <table>
        <thead>
          <tr>
            <th>Параметр</th>
            <th>Тип</th>
            <th>Обяз.</th>
            <th>Описание</th>
            <th>По умолчанию</th>
            <th>Примечания</th>
          </tr>
        </thead>
        <tbody>
          ${entries.map(([name, paramSchema]) => `
            <tr>
              <td><code>${escapeHtml(name)}</code></td>
              <td>${escapeHtml(formatMcpSchemaType(paramSchema))}</td>
              <td>${required.has(name) ? 'Да' : 'Нет'}</td>
              <td>${escapeHtml(schemaDescription(paramSchema))}</td>
              <td>${escapeHtml(formatMcpSchemaDefault(paramSchema))}</td>
              <td>${formatMcpSchemaRules(paramSchema)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function schemaDescription(schema) {
  if (!schema || typeof schema !== 'object') return '—';
  return schema.description || schema.title || '—';
}

function formatMcpSchemaType(schema) {
  if (!schema || typeof schema !== 'object') return '—';
  if (Array.isArray(schema.anyOf)) return joinSchemaTypes(schema.anyOf);
  if (Array.isArray(schema.oneOf)) return joinSchemaTypes(schema.oneOf);
  if (Array.isArray(schema.type)) return schema.type.join(' | ');
  if (schema.type === 'array') return `array<${formatMcpSchemaType(schema.items || {})}>`;
  if (schema.type === 'object') return 'object';
  if (schema.type) return String(schema.type);
  if (Array.isArray(schema.enum)) return 'enum';
  return '—';
}

function joinSchemaTypes(items) {
  const values = items
    .map((item) => formatMcpSchemaType(item))
    .filter((value) => value && value !== '—');
  return Array.from(new Set(values)).join(' | ') || '—';
}

function collectSchemaEnums(schema, target = []) {
  if (!schema || typeof schema !== 'object') return target;
  if (Array.isArray(schema.enum)) target.push(...schema.enum);
  for (const key of ['anyOf', 'oneOf']) {
    if (Array.isArray(schema[key])) schema[key].forEach((item) => collectSchemaEnums(item, target));
  }
  return target;
}

function collectArrayItemSchemas(schema, target = []) {
  if (!schema || typeof schema !== 'object') return target;
  if (schema.type === 'array' && schema.items && typeof schema.items === 'object') {
    target.push(schema.items);
  }
  for (const key of ['anyOf', 'oneOf']) {
    if (Array.isArray(schema[key])) schema[key].forEach((item) => collectArrayItemSchemas(item, target));
  }
  return target;
}

function collectArrayItemEnums(schema) {
  return collectArrayItemSchemas(schema)
    .flatMap((itemSchema) => collectSchemaEnums(itemSchema, []));
}

function collectArrayItemTypes(schema) {
  return collectArrayItemSchemas(schema)
    .map((itemSchema) => formatMcpSchemaType(itemSchema))
    .filter((value) => value && value !== '—');
}

function formatSchemaValue(value) {
  if (value === null) return 'null';
  if (typeof value === 'string') return `"${value}"`;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return prettyJson(value);
}

function formatMcpSchemaDefault(schema) {
  if (!schema || typeof schema !== 'object') return '—';
  return Object.prototype.hasOwnProperty.call(schema, 'default')
    ? formatSchemaValue(schema.default)
    : '—';
}

function formatMcpSchemaRules(schema) {
  if (!schema || typeof schema !== 'object') return '—';
  const rules = [];
  const enumValues = collectSchemaEnums(schema);
  const itemEnumValues = collectArrayItemEnums(schema);
  const itemTypes = collectArrayItemTypes(schema);
  if (enumValues.length) {
    rules.push(`Допустимо: ${Array.from(new Set(enumValues.map(formatSchemaValue))).join(', ')}`);
  }
  if (itemEnumValues.length) {
    rules.push(`Допустимые значения массива: ${Array.from(new Set(itemEnumValues.map(formatSchemaValue))).join(', ')}`);
  } else if (itemTypes.length) {
    rules.push(`Тип элементов массива: ${Array.from(new Set(itemTypes)).join(' | ')}`);
  }
  if (schema.minimum !== undefined) rules.push(`min: ${formatSchemaValue(schema.minimum)}`);
  if (schema.maximum !== undefined) rules.push(`max: ${formatSchemaValue(schema.maximum)}`);
  if (schema.minLength !== undefined) rules.push(`min length: ${formatSchemaValue(schema.minLength)}`);
  if (schema.maxLength !== undefined) rules.push(`max length: ${formatSchemaValue(schema.maxLength)}`);
  if (schema.pattern) rules.push(`pattern: ${formatSchemaValue(schema.pattern)}`);
  if (!rules.length) return '—';
  return rules.map((rule) => `<span class="mcp-param-rule">${escapeHtml(rule)}</span>`).join('');
}

function renderMcpReturnsTab(tool) {
  const doc = tool.return_doc || {};
  const returns = Array.isArray(doc.returns) ? doc.returns : [];
  const notes = Array.isArray(doc.notes) ? doc.notes : [];
  if (!tool.documented || !returns.length) {
    return '<div class="mcp-tools-empty">Для этого tool пока нет описания результата.</div>';
  }

  return `
    <div class="mcp-tool-tab-fill mcp-tool-doc-tab">
      <div class="mcp-tool-return-summary">${escapeHtml(doc.summary || '')}</div>
      <div class="mcp-tool-return-list">
        ${returns.map((item, idx) => `
          <section class="mcp-tool-return-case">
            <h4>${escapeHtml(item.case || `Вариант ${idx + 1}`)}</h4>
            ${item.shape ? `<div class="mcp-tool-return-shape">${escapeHtml(item.shape)}</div>` : ''}
            ${item.description ? `<p class="mcp-tool-return-description">${escapeHtml(item.description)}</p>` : ''}
            ${renderMcpReturnFields(item.fields)}
          </section>
        `).join('')}
      </div>
      ${notes.length ? `
        <div class="mcp-tool-notes">
          <div class="mcp-tool-notes-title">Примечания</div>
          <ul>${notes.map((note) => `<li>${escapeHtml(note)}</li>`).join('')}</ul>
        </div>
      ` : ''}
    </div>
  `;
}

function renderMcpReturnFields(fields) {
  if (!Array.isArray(fields) || !fields.length) {
    return '<div class="mcp-tools-empty">Поля результата пока не описаны.</div>';
  }
  return `
    <div class="mcp-return-fields">
      <table>
        <thead>
          <tr>
            <th>Поле</th>
            <th>Тип</th>
            <th>Описание</th>
            <th>Когда появляется</th>
          </tr>
        </thead>
        <tbody>
          ${fields.map((field) => `
            <tr>
              <td><code>${escapeHtml(field.name || '')}</code></td>
              <td>${escapeHtml(field.type || '')}</td>
              <td>${escapeHtml(field.description || '')}</td>
              <td>${escapeHtml(field.when || 'Всегда')}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function renderMcpExamplesTab(tool) {
  const doc = tool.return_doc || {};
  const examples = Array.isArray(doc.examples) ? doc.examples : [];
  if (!tool.documented || !examples.length) {
    return '<div class="mcp-tools-empty">Для этого tool пока нет примеров ответа.</div>';
  }

  return `
    <div class="mcp-tool-tab-fill mcp-tool-examples-tab">
      <div class="mcp-tool-return-list">
      ${examples.map((item, idx) => `
        <section class="mcp-tool-return-case">
          <h4>${escapeHtml(item.case || `Пример ${idx + 1}`)}</h4>
          ${renderMcpJsonBlock(item.json, 'example-response.json')}
        </section>
      `).join('')}
      </div>
    </div>
  `;
}

function renderMcpJsonBlock(value, label) {
  return `
    <div class="mcp-json-block">
      <div class="mcp-json-toolbar">
        <span>${escapeHtml(label || 'json')}</span>
        <button type="button" class="mcp-json-copy" data-role="mcp-json-copy"
                title="Скопировать JSON" aria-label="Скопировать JSON">⧉</button>
      </div>
      <pre><code>${escapeHtml(prettyJson(value))}</code></pre>
    </div>
  `;
}

async function handleMcpJsonCopy(button) {
  const block = button.closest('.mcp-json-block');
  const text = block?.querySelector('pre')?.textContent || '';
  if (!text.trim()) return;
  try {
    await copyTextToClipboard(text);
    button.textContent = '✓';
    button.classList.add('copied');
  } catch {
    button.textContent = '!';
    button.classList.add('copy-error');
  }
  window.setTimeout(() => {
    button.textContent = '⧉';
    button.classList.remove('copied', 'copy-error');
  }, 1400);
}

async function refresh() {
  try {
    const [sRes, hRes, usageRes, toolsRes, usersRes] = await Promise.all([
      fetch(withToken(`${API_PREFIX}/stats`)),
      fetch(withToken(`${API_PREFIX}/health`)),
      fetch(withToken(`${API_PREFIX}/runtime/usage?scope=${encodeURIComponent(runtimeUsageState.scope)}`)),
      fetch(withToken(`${API_PREFIX}/mcp/tools`)),
      fetch(withToken(`${API_PREFIX}/users`)),
    ]);

    hideError();

    if (hRes.ok) {
      const health = await hRes.json();
      const summaryEl = $('system-summary');
      const summary = [];
      if (health.project_name) summary.push(`Project: ${health.project_name}`);
      if (health.config_name) summary.push(`Configuration: ${health.config_name}`);
      summaryEl.textContent = summary.join(' · ');
      renderBadges(health);
    }

    if (toolsRes.ok) {
      renderMcpToolsSection(await toolsRes.json());
    } else {
      const err = await toolsRes.json().catch(() => ({}));
      renderMcpToolsError(`MCP tools ${toolsRes.status}: ${err.error || err.message || toolsRes.statusText}`);
    }

    if (usageRes.ok) {
      renderRuntimeUsageSection(await usageRes.json());
    } else {
      const err = await usageRes.json().catch(() => ({}));
      renderRuntimeUsageError(`Статистика ${usageRes.status}: ${err.error || err.message || usageRes.statusText}`);
    }

    if (usersRes.ok) {
      renderConsoleUsersSection(await usersRes.json());
    } else {
      const err = await usersRes.json().catch(() => ({}));
      renderConsoleUsersError(`Пользователи ${usersRes.status}: ${err.error || err.message || usersRes.statusText}`);
    }

    if (!sRes.ok) {
      const err = await sRes.json().catch(() => ({}));
      showError(`Stats ${sRes.status}: ${err.error || sRes.statusText}`);
      return;
    }

    const data = await sRes.json();
    renderStats(data);
    const ts = formatRuDateTime(data.updated_at);
    $('updated').textContent = `Статистика на: ${ts}`;
  } catch (err) {
    showError(`Network error: ${err.message}`);
  }
}

// Manual admin-only refresh: POST /stats/refresh пересчитывает кеш через loader и
// обновляет только карточки статистики + timestamp (остальные секции не трогаются).
async function refreshStatsManually() {
  const btn = $('stats-refresh-btn');
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(withToken(`${API_PREFIX}/stats/refresh`), { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showError(`Stats ${res.status}: ${err.error || err.message || res.statusText}`);
      return;
    }
    const data = await res.json();
    hideError();
    renderStats(data);
    const ts = formatRuDateTime(data.updated_at);
    $('updated').textContent = `Статистика на: ${ts}`;
  } catch (err) {
    showError(`Network error: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function initAnalysis() {
  const params = new URLSearchParams(window.location.search);
  const restore = savedSelectionFromUrl(params) || readAnalysisPageState();
  analysisState.pendingRestore = restore;
  analysisState.tab = normalizeObjectTab(restore?.tab || params.get('tab'));
  setupAnalysisEvents();
  setupResizeEvents();
  setupAgentPanel();
  await loadAnalysisTree();
}

function setupAnalysisEvents() {
  const tree = $('analysis-tree');
  const detail = $('analysis-detail');
  const filter = $('analysis-filter');
  const searchSettings = $('analysis-search-settings');

  if (tree && !tree.dataset.bound) {
    tree.dataset.bound = '1';
    tree.addEventListener('click', (event) => {
      const toggle = event.target.closest('button[data-toggle]');
      if (toggle) {
        toggleTreeBranch(toggle);
        return;
      }
      const button = event.target.closest('button[data-kind]');
      if (!button) return;
      const kind = button.dataset.kind;
      if (kind === 'project') renderProjectDetail();
      if (kind === 'config') renderConfigDetail(button.dataset.config);
      if (kind === 'category-group') renderCategoryGroupDetail(button.dataset.config, button.dataset.group);
      if (kind === 'category') selectCategory(button, { expand: false });
      if (kind === 'object') selectObject(button.dataset.ref);
      if (kind === 'object-section') selectObjectSection(button.dataset.ref, button.dataset.section);
      if (kind === 'node') selectNode(button.dataset.ref);
      if (kind === 'module') selectModule({
        moduleId: button.dataset.moduleId,
        ownerRef: button.dataset.ownerRef,
        moduleType: button.dataset.moduleType,
      });
      if (kind === 'load-more') loadMoreCategory(button.dataset.config, button.dataset.category);
      if (kind === 'search-load-more') loadMoreSearchResults();
    });
  }

  if (detail && !detail.dataset.bound) {
    detail.dataset.bound = '1';
    detail.addEventListener('click', (event) => {
      const actionButton = event.target.closest('button[data-action]');
      if (actionButton?.dataset.action === 'back') {
        goBackSelection();
        return;
      }
      const tabButton = event.target.closest('button[data-tab]');
      if (tabButton) {
        analysisState.tab = normalizeObjectTab(tabButton.dataset.tab);
        renderObjectTab();
        if (analysisState.selectedRef) setAnalysisUrlRef(analysisState.selectedRef);
        return;
      }
      const kindButton = event.target.closest('button[data-kind]');
      if (kindButton) {
        const kind = kindButton.dataset.kind;
        if (kind === 'project') renderProjectDetail();
        if (kind === 'config') renderConfigDetail(kindButton.dataset.config);
        if (kind === 'category-group') renderCategoryGroupDetail(kindButton.dataset.config, kindButton.dataset.group);
        if (kind === 'category') selectCategory(kindButton, { expand: true });
        if (kind === 'object') selectObject(kindButton.dataset.ref);
        if (kind === 'object-section') selectObjectSection(kindButton.dataset.ref, kindButton.dataset.section);
        if (kind === 'node') selectNode(kindButton.dataset.ref);
        if (kind === 'module') selectModule({
          moduleId: kindButton.dataset.moduleId,
          ownerRef: kindButton.dataset.ownerRef,
          moduleType: kindButton.dataset.moduleType,
        });
        if (kind === 'load-more') loadMoreCategory(kindButton.dataset.config, kindButton.dataset.category);
        return;
      }
      const nodeButton = event.target.closest('button[data-node-ref]');
      if (nodeButton) {
        selectNode(nodeButton.dataset.nodeRef);
        return;
      }
      const conditionButton = event.target.closest('button[data-right-condition]');
      if (conditionButton) {
        event.preventDefault();
        event.stopPropagation();
        showRightConditionModal(conditionButton.dataset.rightCondition);
        return;
      }
      const extensionFilterButton = event.target.closest('button[data-extension-filter]');
      if (extensionFilterButton) {
        event.preventDefault();
        event.stopPropagation();
        toggleExtensionFilter(extensionFilterButton);
        return;
      }
      const refButton = event.target.closest('[data-ref]');
      if (refButton) selectObject(refButton.dataset.ref);
    });
  }

  if (filter && !filter.dataset.bound) {
    filter.dataset.bound = '1';
    filter.addEventListener('input', () => scheduleAnalysisSearch(filter.value));
    filter.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      runAnalysisSearchNow(filter.value);
    });
  }

  if (searchSettings && !searchSettings.dataset.bound) {
    searchSettings.dataset.bound = '1';
    searchSettings.addEventListener('click', showSearchSettingsModal);
  }
}

function setupResizeEvents() {
  const workspace = document.querySelector('.analysis-workspace');
  if (!workspace) return;
  setupTreeResize(workspace);
  setupDetailPanelToggle(workspace);
  setupAgentResize(workspace);
}

function setupDetailPanelToggle(workspace) {
  const toggle = $('detail-toggle');
  if (!toggle) return;
  const collapsed = localStorage.getItem('analysisDetailCollapsed') === '1';
  setDetailCollapsed(collapsed);
  if (!toggle.dataset.bound) {
    toggle.dataset.bound = '1';
    toggle.addEventListener('click', () => {
      setDetailCollapsed(!workspace.classList.contains('detail-collapsed'));
    });
  }
}

function setupTreeResize(workspace) {
  const handle = $('tree-resizer');
  if (!handle || handle.dataset.bound) return;
  handle.dataset.bound = '1';

  const storedWidth = Number(localStorage.getItem('analysisTreeWidth') || 0);
  if (storedWidth) {
    workspace.style.setProperty('--tree-width', `${Math.max(260, Math.min(930, storedWidth))}px`);
  }

  handle.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('is-resizing-tree');
  });

  handle.addEventListener('pointermove', (event) => {
    if (!document.body.classList.contains('is-resizing-tree')) return;
    const rect = workspace.getBoundingClientRect();
    const width = Math.max(260, Math.min(930, event.clientX - rect.left));
    workspace.style.setProperty('--tree-width', `${width}px`);
    localStorage.setItem('analysisTreeWidth', String(Math.round(width)));
    const agentPanel = document.querySelector('.agent-panel');
    if (agentPanel && !workspace.classList.contains('agent-collapsed')) {
      const agentWidth = clampAgentWidth(workspace, Math.round(agentPanel.getBoundingClientRect().width || 0));
      workspace.style.setProperty('--agent-expanded-width', `${agentWidth}px`);
      localStorage.setItem('analysisAgentWidth', String(Math.round(agentWidth)));
    }
  });

  handle.addEventListener('pointerup', (event) => {
    handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove('is-resizing-tree');
  });
}

function setupAgentResize(workspace) {
  const handle = $('agent-resizer');
  const panel = document.querySelector('.agent-panel');
  if (!handle || !panel || handle.dataset.bound) return;
  handle.dataset.bound = '1';

  const defaultWidth = Math.round(panel.getBoundingClientRect().width || 0);
  if (defaultWidth > 0) {
    workspace.dataset.agentDefaultWidth = String(defaultWidth);
  }

  const storedWidth = Number(localStorage.getItem('analysisAgentWidth') || 0);
  if (storedWidth) {
    applyStoredAgentWidth(workspace);
  }

  const reclampAgentWidth = () => {
    if (workspace.classList.contains('agent-collapsed')) return;
    const currentWidth = Math.round(panel.getBoundingClientRect().width || 0);
    if (!currentWidth) return;
    const width = clampAgentWidth(workspace, currentWidth);
    if (width !== currentWidth) {
      workspace.style.setProperty('--agent-expanded-width', `${width}px`);
      localStorage.setItem('analysisAgentWidth', String(Math.round(width)));
    }
  };
  window.addEventListener('resize', reclampAgentWidth);
  requestAnimationFrame(() => applyStoredAgentWidth(workspace));

  let startX = 0;
  let startWidth = 0;

  handle.addEventListener('pointerdown', (event) => {
    if (workspace.classList.contains('agent-collapsed')) return;
    event.preventDefault();
    startX = event.clientX;
    startWidth = Math.round(panel.getBoundingClientRect().width || 0);
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('is-resizing-agent');
  });

  handle.addEventListener('pointermove', (event) => {
    if (!document.body.classList.contains('is-resizing-agent')) return;
    const width = clampAgentWidth(workspace, startWidth - (event.clientX - startX));
    workspace.style.setProperty('--agent-expanded-width', `${width}px`);
    localStorage.setItem('analysisAgentWidth', String(Math.round(width)));
  });

  handle.addEventListener('pointerup', (event) => {
    handle.releasePointerCapture(event.pointerId);
    document.body.classList.remove('is-resizing-agent');
  });
}

function clampAgentWidth(workspace, width) {
  const defaultWidth = Number(workspace.dataset.agentDefaultWidth || 0) || 640;
  const style = getComputedStyle(workspace);
  const gap = parseFloat(style.columnGap || style.gap || 0) || 0;
  const treeWidth = parseFloat(style.getPropertyValue('--tree-width')) || 340;
  const treeResizerWidth = 8;
  const agentResizerWidth = parseFloat(style.getPropertyValue('--agent-resizer-width')) || 8;
  const detailMinWidth = workspace.classList.contains('detail-collapsed')
    ? 46
    : (parseFloat(style.getPropertyValue('--detail-min-width')) || 420);
  const gapsWidth = gap * 4;
  const availableWidth = Math.floor(
    workspace.clientWidth - treeWidth - treeResizerWidth - agentResizerWidth - detailMinWidth - gapsWidth,
  );
  const maxWidth = Math.max(46, availableWidth);
  const minWidth = Math.min(Math.max(320, Math.round(defaultWidth / 2)), maxWidth);
  return Math.max(minWidth, Math.min(maxWidth, Math.round(width)));
}

function applyStoredAgentWidth(workspace) {
  if (!workspace || workspace.classList.contains('agent-collapsed')) return 0;
  const storedWidth = Number(localStorage.getItem('analysisAgentWidth') || 0);
  if (!storedWidth) return 0;
  const width = clampAgentWidth(workspace, storedWidth);
  workspace.style.setProperty('--agent-expanded-width', `${width}px`);
  return width;
}

function setupAgentPanel() {
  const workspace = document.querySelector('.analysis-workspace');
  const toggle = $('agent-toggle');
  const form = $('agent-form');
  const input = $('agent-input');
  const chatList = $('agent-chat-list');
  const newChat = $('agent-new-chat');
  const modelDropdown = $('agent-model-dropdown');
  const modelButton = $('agent-model-button');
  const modelMenu = $('agent-model-menu');
  const chatPopover = $('agent-chat-popover');
  const commandMenu = $('agent-command-menu');
  const commandTrigger = $('agent-command-trigger');
  const sendContext = $('agent-send-context');
  if (!workspace || !toggle) return;

  const collapsed = localStorage.getItem('analysisAgentCollapsed') === '1';
  setAgentCollapsed(collapsed);
  setupAgentModelSelector();
  renderAgentInitialState();
  if (AGENT_ENABLED && !agentState.chatsInitialized) {
    agentState.chatsInitialized = true;
    initializeAgentChats();
  } else {
    renderAgentUsage();
  }

  if (!toggle.dataset.bound) {
    toggle.dataset.bound = '1';
    toggle.addEventListener('click', () => {
      setAgentCollapsed(!workspace.classList.contains('agent-collapsed'));
    });
  }

  if (newChat && !newChat.dataset.bound) {
    newChat.dataset.bound = '1';
    newChat.addEventListener('click', () => startNewAgentChat());
  }

  if (chatList && !chatList.dataset.bound) {
    chatList.dataset.bound = '1';
    chatList.addEventListener('click', (event) => {
      event.stopPropagation();
      toggleAgentChatList();
    });
  }

  if (modelButton && !modelButton.dataset.bound) {
    modelButton.dataset.bound = '1';
    modelButton.addEventListener('click', (event) => {
      event.stopPropagation();
      const menuOpen = !$('agent-model-menu')?.classList.contains('hidden');
      setAgentModelDropdownOpen(!menuOpen);
    });
  }

  if (modelMenu && !modelMenu.dataset.bound) {
    modelMenu.dataset.bound = '1';
    modelMenu.addEventListener('click', (event) => {
      event.stopPropagation();
      const option = event.target.closest('.agent-model-option');
      if (!option) return;
      changeAgentLlmProfile(option.dataset.profileId);
    });
  }

  if (chatPopover && !chatPopover.dataset.bound) {
    chatPopover.dataset.bound = '1';
    chatPopover.addEventListener('click', handleAgentChatPopoverClick);
  }

  if (form && input && !form.dataset.bound) {
    form.dataset.bound = '1';
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (message === '/') {
        setAgentCommandMenuOpen(true);
        return;
      }
      if (!message || agentState.busy || !AGENT_ENABLED) return;
      input.value = '';
      resizeAgentInput();
      setAgentCommandMenuOpen(false);
      sendAgentMessage(message);
    });
    const send = $('agent-send');
    if (send && !send.dataset.bound) {
      send.dataset.bound = '1';
      send.addEventListener('click', (event) => {
        if (!agentState.busy) return;
        event.preventDefault();
        stopAgentRun();
      });
    }
    input.addEventListener('input', resizeAgentInput);
    input.addEventListener('input', () => {
      setAgentCommandMenuOpen(input.value.trim() === '/');
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        setAgentCommandMenuOpen(false);
        return;
      }
      if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) {
        return;
      }
      event.preventDefault();
      if (agentState.busy || !AGENT_ENABLED) return;
      const message = input.value.trim();
      if (message === '/') {
        setAgentCommandMenuOpen(true);
        return;
      }
      if (!message) return;
      input.value = '';
      resizeAgentInput();
      setAgentCommandMenuOpen(false);
      sendAgentMessage(message);
    });
    resizeAgentInput();
  }

  if (input && !input.dataset.formatMenuBound) {
    input.dataset.formatMenuBound = '1';
    window.agentCodeFormatMenu = new AgentCodeFormatMenu(input, $('agent-format-menu'));
  }

  if (commandTrigger && !commandTrigger.dataset.bound) {
    commandTrigger.dataset.bound = '1';
    commandTrigger.addEventListener('click', (event) => {
      event.preventDefault();
      setAgentCommandMenuOpen($('agent-command-menu')?.classList.contains('hidden'));
      input?.focus();
    });
  }

  if (commandMenu && !commandMenu.dataset.bound) {
    commandMenu.dataset.bound = '1';
    commandMenu.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-agent-action]');
      if (!button || agentState.busy || !AGENT_ENABLED) return;
      if (!readAgentSendContextEnabled()) return;
      const message = agentQuickActionMessage(button.dataset.agentAction);
      if (message) {
        if (input && input.value.trim() === '/') input.value = '';
        setAgentCommandMenuOpen(false);
        sendAgentMessage(message);
      }
    });
  }

  if (sendContext) {
    sendContext.checked = readAgentSendContextEnabled();
    if (!sendContext.dataset.bound) {
      sendContext.dataset.bound = '1';
      sendContext.addEventListener('change', () => {
        writeAgentSendContextEnabled(sendContext.checked);
        updateAgentControls();
      });
    }
  }

  const body = $('agent-body');
  if (body && !body.dataset.copyBound) {
    body.dataset.copyBound = '1';
    body.addEventListener('click', handleAgentMessageCopyClick);
    body.addEventListener('click', handleAgentConsoleLinkClick);
    body.addEventListener('click', handleAgentToolOpenClick);
  }

  if (body && !body.dataset.scrollBound) {
    body.dataset.scrollBound = '1';
    body.addEventListener('scroll', () => {
      agentState.autoScroll = isAgentBodyNearBottom(body);
    }, { passive: true });
    agentState.autoScroll = isAgentBodyNearBottom(body);
  }
  updateAgentControls();

  if (!window.__consoleAgentPersistBound) {
    window.__consoleAgentPersistBound = true;
    window.addEventListener('beforeunload', persistAgentTranscriptNow);
    document.addEventListener('click', (event) => {
      const popover = $('agent-chat-popover');
      const button = $('agent-chat-list');
      if (agentState.chatListOpen && !popover?.contains(event.target) && !button?.contains(event.target)) {
        setAgentChatListOpen(false);
      }
      if (modelDropdown && !modelDropdown.contains(event.target)) {
        setAgentModelDropdownOpen(false);
      }
      const commandMenu = $('agent-command-menu');
      const commandTrigger = $('agent-command-trigger');
      if (commandMenu && commandTrigger
          && !commandMenu.contains(event.target)
          && !commandTrigger.contains(event.target)
          && !input?.contains(event.target)) {
        setAgentCommandMenuOpen(false);
      }
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        setAgentModelDropdownOpen(false);
        setAgentCommandMenuOpen(false);
      }
    });
  }
}

function renderAgentInitialState() {
  const body = $('agent-body');
  if (!body) return;
  if (!AGENT_ENABLED) {
    body.innerHTML = '<div class="agent-disabled">Агент Метакод отключен</div>';
    return;
  }
  if (!body.children.length) {
    body.innerHTML = '<div class="agent-empty">Загрузка чатов...</div>';
  }
}

function updateAgentControls() {
  syncAgentBusy();
  const input = $('agent-input');
  const send = $('agent-send');
  const chatList = $('agent-chat-list');
  const newChat = $('agent-new-chat');
  const modelButton = $('agent-model-button');
  const commandMenu = $('agent-command-menu');
  const commandTrigger = $('agent-command-trigger');
  const sendContext = $('agent-send-context');
  const inputDisabled = !AGENT_ENABLED || agentState.busy || agentState.loadingChat;
  const actionDisabled = !AGENT_ENABLED;
  if (input) {
    input.disabled = inputDisabled;
    resizeAgentInput();
  }
  if (send) {
    send.disabled = actionDisabled;
    send.classList.toggle('is-stop-mode', Boolean(agentState.busy));
    send.title = agentState.busy ? 'Остановить' : 'Отправить';
    send.setAttribute('aria-label', agentState.busy ? 'Остановить' : 'Отправить');
    send.type = agentState.busy ? 'button' : 'submit';
  }
  if (commandTrigger) {
    commandTrigger.disabled = inputDisabled;
    commandTrigger.setAttribute('aria-expanded', commandMenu && !commandMenu.classList.contains('hidden') ? 'true' : 'false');
  }
  if (inputDisabled) setAgentCommandMenuOpen(false);
  if (chatList) {
    chatList.disabled = !AGENT_ENABLED || agentState.loadingChat;
    chatList.setAttribute('aria-expanded', agentState.chatListOpen ? 'true' : 'false');
    updateAgentChatListRunningIndicator();
  }
  if (newChat) newChat.disabled = !AGENT_ENABLED || agentState.loadingChat;
  if (modelButton) {
    modelButton.disabled = !AGENT_ENABLED || agentState.loadingChat || agentState.busy || agentState.llmProfiles.length <= 1;
    if (modelButton.disabled) setAgentModelDropdownOpen(false);
  }
  if (commandMenu) {
    commandMenu.querySelectorAll('button').forEach((button) => {
      const message = agentQuickActionMessage(button.dataset.agentAction);
      const contextEnabled = readAgentSendContextEnabled();
      button.disabled = inputDisabled || !contextEnabled || !message;
      button.title = contextEnabled
        ? (message || 'Недоступно для текущего выбора')
        : 'Включите "Контекст страницы"';
    });
  }
  if (sendContext) sendContext.disabled = inputDisabled;
}

function setAgentCommandMenuOpen(open) {
  const menu = $('agent-command-menu');
  const trigger = $('agent-command-trigger');
  const input = $('agent-input');
  const shouldOpen = Boolean(open) && AGENT_ENABLED && !agentState.busy && !agentState.loadingChat && !input?.disabled;
  if (menu) menu.classList.toggle('hidden', !shouldOpen);
  if (trigger) trigger.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
}

function formatAgentTokenCount(value) {
  const number = Number(value || 0);
  if (!Number.isFinite(number) || number <= 0) return '0';
  return Math.round(number).toLocaleString('ru-RU');
}

function formatAgentCost(amount, unit) {
  const number = Number(amount);
  if (!Number.isFinite(number) || number <= 0) return '';
  const formatted = number < 0.0001
    ? number.toExponential(2)
    : number.toLocaleString('ru-RU', {
      minimumFractionDigits: 0,
      maximumFractionDigits: 6,
    });
  const normalizedUnit = String(unit || '').trim();
  if (!normalizedUnit) return formatted;
  if (normalizedUnit === 'usd' || normalizedUnit === 'credits') return `$${formatted}`;
  return `${formatted} ${normalizedUnit}`;
}

function normalizeAgentUsageForDisplay(nextUsage, previousUsage) {
  if (!nextUsage) return nextUsage;
  const nextContext = nextUsage.context || {};
  if (nextContext.source !== 'estimated_local') return nextUsage;

  const nextTokens = Number(nextContext.tokens || 0);
  const previousContext = previousUsage?.context || {};
  const previousTokens = Number(previousContext.tokens || 0);
  if (!Number.isFinite(previousTokens) || previousTokens <= nextTokens) return nextUsage;

  return {
    ...nextUsage,
    context: {
      ...nextContext,
      tokens: previousTokens,
      source: previousContext.source || nextContext.source,
    },
  };
}

function renderAgentUsage(payload = agentState.usage) {
  const el = $('agent-token-usage');
  if (!el) return;
  let usageToRender = payload;
  if (payload) {
    usageToRender = normalizeAgentUsageForDisplay(payload, agentState.usage);
    agentState.usage = usageToRender;
    persistAgentUsage(usageToRender);
  }
  const usage = usageToRender || agentState.usage;
  if (!usage) {
    el.textContent = 'Токены: 0';
    const profile = currentAgentLlmProfile();
    el.title = [
      'Токены текущей сессии и размер последнего контекста модели',
      profile?.model ? `Модель: ${profile.model}` : '',
    ].filter(Boolean).join('\n');
    return;
  }

  const session = usage.session || {};
  const context = usage.context || {};
  const llmProfile = usage.llm_profile || {};
  const currentProfile = currentAgentLlmProfile();
  const model = String(usage.model || llmProfile.model || currentProfile?.model || '').trim();
  const profileTitle = String(llmProfile.profile_title || currentProfile?.title || '').trim();
  const endpointTitle = String(llmProfile.endpoint_title || currentProfile?.endpointTitle || '').trim();
  const input = formatAgentTokenCount(session.input_tokens);
  const output = formatAgentTokenCount(session.output_tokens);
  const total = formatAgentTokenCount(session.total_tokens);
  const contextPrefix = context.source === 'estimated_local' ? '~' : '';
  const contextText = context.tokens ? `${contextPrefix}${formatAgentTokenCount(context.tokens)}` : '0';
  const cost = formatAgentCost(session.cost_amount, session.cost_unit);
  el.textContent = `Σ ${total} · in ${input} · out ${output} · ctx ${contextText}${cost ? ` · ${cost}` : ''}`;
  el.title = [
    profileTitle ? `Профиль: ${profileTitle}` : '',
    endpointTitle ? `Endpoint: ${endpointTitle}` : '',
    model ? `Модель: ${model}` : '',
    `Сессия: ${total} ток.`,
    `Отправлено модели: ${input}`,
    `Получено от модели: ${output}`,
    `Текущий контекст: ${contextText}`,
    cost ? `Стоимость сессии: ${cost}` : '',
    usage.turn?.cost_amount ? `Стоимость последнего запроса: ${formatAgentCost(usage.turn.cost_amount, usage.turn.cost_unit)}` : '',
  ].filter(Boolean).join('\n');
}

function clearAgentActiveRunState() {
  agentState.activeAssistantMessageEl = null;
  agentState.activeAssistantTextEl = null;
  agentState.activeAssistantReasoningEl = null;
  agentState.activeAssistantPlanEl = null;
  agentState.activeAssistantStepsEl = null;
  agentState.pendingToolIds = [];
  agentState.toolSeq = 0;
  agentState.activeAnswerRaw = '';
  agentState.activeAnswer = '';
  agentState.activeReasoning = '';
}

function activeAgentRunningTurn() {
  return agentState.runningByChat[agentState.chatId] || null;
}

function syncAgentBusy() {
  agentState.busy = Boolean(activeAgentRunningTurn());
  return agentState.busy;
}

function hasOtherRunningAgentChats() {
  const activeChatId = String(agentState.chatId || '');
  const runningChatIds = new Set(
    Object.keys(agentState.runningByChat || {}).filter(Boolean),
  );
  if (Array.isArray(agentState.chats)) {
    agentState.chats.forEach((chat) => {
      const chatId = String(chat?.id || '');
      if (chatId && chat?.running) runningChatIds.add(chatId);
    });
  }
  runningChatIds.delete(activeChatId);
  return runningChatIds.size > 0;
}

function hasKnownRunningAgentChats() {
  if (Object.keys(agentState.runningByChat || {}).length > 0) return true;
  return Array.isArray(agentState.chats) && agentState.chats.some((chat) => Boolean(chat?.running));
}

function syncAgentRunningChatsRefresh() {
  const hasRunning = hasKnownRunningAgentChats();
  if (!hasRunning && agentState.runningRefreshTimer) {
    window.clearTimeout(agentState.runningRefreshTimer);
    agentState.runningRefreshTimer = null;
    return;
  }
  if (!AGENT_ENABLED || !hasRunning || agentState.runningRefreshTimer) return;
  agentState.runningRefreshTimer = window.setTimeout(async () => {
    agentState.runningRefreshTimer = null;
    if (!hasKnownRunningAgentChats()) {
      updateAgentChatListRunningIndicator();
      return;
    }
    await refreshAgentChatsSilently();
  }, AGENT_RUNNING_CHATS_REFRESH_MS);
}

function updateAgentChatListRunningIndicator() {
  const button = $('agent-chat-list');
  if (!button) return;
  const hasOtherRunning = hasOtherRunningAgentChats();
  button.classList.toggle('has-other-running', hasOtherRunning);
  button.title = hasOtherRunning ? 'Чаты: есть выполняющиеся задачи в других чатах' : 'Чаты';
  button.setAttribute(
    'aria-label',
    hasOtherRunning ? 'Чаты, есть выполняющиеся задачи в других чатах' : 'Чаты',
  );
}

function markAgentChatRunningState(chatId, running, turnId = '') {
  const chatKey = String(chatId || '');
  if (!chatKey || !Array.isArray(agentState.chats)) return;
  agentState.chats = agentState.chats.map((chat) => {
    if (String(chat?.id || '') !== chatKey) return chat;
    return {
      ...chat,
      running: Boolean(running),
      running_turn_id: running ? String(turnId || chat.running_turn_id || '') : '',
      last_turn_status: running
        ? 'running'
        : (String(chat.last_turn_status || '') === 'running' ? '' : String(chat.last_turn_status || '')),
    };
  });
}

function renderAgentChatListIfOpen() {
  if (agentState.chatListOpen) renderAgentChatList();
}

function setAgentRunning(chatId, turnId) {
  const chatKey = String(chatId || '');
  const turnKey = String(turnId || '');
  if (!chatKey || !turnKey) return;
  agentState.runningByChat[chatKey] = { turnId: turnKey };
  markAgentChatRunningState(chatKey, true, turnKey);
  if (chatKey === agentState.chatId) {
    agentState.activeTurnId = turnKey;
  }
  syncAgentBusy();
  updateAgentChatListRunningIndicator();
  syncAgentRunningChatsRefresh();
  renderAgentChatListIfOpen();
}

function clearAgentRunning(chatId, turnId = '') {
  const chatKey = String(chatId || '');
  if (!chatKey) return;
  const current = agentState.runningByChat[chatKey];
  if (!current) return;
  if (turnId && String(current.turnId || '') !== String(turnId)) return;
  delete agentState.runningByChat[chatKey];
  if (chatKey === agentState.chatId && (!turnId || agentState.activeTurnId === String(turnId))) {
    agentState.activeTurnId = '';
  }
  markAgentChatRunningState(chatKey, false);
  syncAgentBusy();
  updateAgentChatListRunningIndicator();
  syncAgentRunningChatsRefresh();
  renderAgentChatListIfOpen();
}

function detachAgentTurnStream() {
  cacheAgentCurrentView();
  if (agentState.streamController) {
    agentState.streamRunId += 1;
    agentState.streamController.abort();
  }
  agentState.streamController = null;
  clearAgentActiveRunState();
  syncAgentBusy();
}

function abortAgentRunForChatChange() {
  detachAgentTurnStream();
}

function activateAgentChat(chatId) {
  const id = String(chatId || '');
  agentState.chatId = id;
  agentState.sessionId = id || createAgentSessionId();
  agentState.activeTurnId = agentState.runningByChat[id]?.turnId || '';
  syncAgentBusy();
  updateAgentChatListRunningIndicator();
  syncAgentRunningChatsRefresh();
  if (id) {
    writeActiveAgentChatId(id);
    writeAgentSessionId(id);
  }
}

function setAgentChatListOpen(open) {
  agentState.chatListOpen = Boolean(open);
  if (agentState.chatListOpen) setAgentModelDropdownOpen(false);
  const popover = $('agent-chat-popover');
  const button = $('agent-chat-list');
  if (popover) {
    popover.classList.toggle('hidden', !agentState.chatListOpen);
    if (agentState.chatListOpen) renderAgentChatList();
  }
  if (button) button.setAttribute('aria-expanded', agentState.chatListOpen ? 'true' : 'false');
}

function toggleAgentChatList() {
  if (!AGENT_ENABLED || agentState.loadingChat) return;
  setAgentChatListOpen(!agentState.chatListOpen);
}

function agentChatSortValue(chat) {
  const value = chat?.last_message_at || chat?.updated_at || chat?.created_at || '';
  const time = new Date(value).getTime();
  return Number.isFinite(time) ? time : 0;
}

function renderAgentChatList() {
  const popover = $('agent-chat-popover');
  if (!popover) return;
  const chats = Array.isArray(agentState.chats) ? [...agentState.chats] : [];
  chats.sort((a, b) => agentChatSortValue(b) - agentChatSortValue(a));
  if (!chats.length) {
    popover.innerHTML = '<div class="agent-chat-list-empty">Чатов пока нет</div>';
    return;
  }

  popover.innerHTML = '';
  const list = document.createElement('div');
  list.className = 'agent-chat-list-items';
  chats.forEach((chat) => {
    const row = document.createElement('div');
    row.className = 'agent-chat-list-item';
    const running = Boolean(chat.running || agentState.runningByChat[chat.id]);
    if (chat.id === agentState.chatId) row.classList.add('is-active');
    if (running) row.classList.add('is-running');
    row.dataset.chatId = chat.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'agent-chat-list-main';
    main.dataset.chatSelect = chat.id;

    const titleRow = document.createElement('span');
    titleRow.className = 'agent-chat-list-title-row';
    if (running) {
      const runningIcon = document.createElement('span');
      runningIcon.className = 'agent-chat-running-indicator';
      runningIcon.title = 'Агент выполняет задачу';
      runningIcon.setAttribute('aria-label', 'Агент выполняет задачу');
      titleRow.appendChild(runningIcon);
    }
    const title = document.createElement('span');
    title.className = 'agent-chat-list-title';
    title.textContent = chat.title || 'Новый чат';
    titleRow.appendChild(title);
    main.appendChild(titleRow);

    const preview = document.createElement('span');
    preview.className = 'agent-chat-list-preview';
    preview.textContent = chat.preview || 'Нет сообщений';
    main.appendChild(preview);

    const meta = document.createElement('span');
    meta.className = 'agent-chat-list-meta';
    meta.textContent = [
      formatRuDateTime(chat.last_message_at || chat.updated_at || chat.created_at),
      running ? 'выполняется' : '',
    ].filter(Boolean).join(' · ');
    main.appendChild(meta);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'agent-chat-delete';
    del.dataset.chatDelete = chat.id;
    del.title = 'Удалить чат';
    del.setAttribute('aria-label', 'Удалить чат');
    del.textContent = '×';
    del.disabled = running;

    row.appendChild(main);
    row.appendChild(del);
    list.appendChild(row);
  });
  popover.appendChild(list);
}

async function refreshAgentChats() {
  const payload = await fetchAgentChats();
  agentState.chats = Array.isArray(payload.chats) ? payload.chats : [];
  mergeAgentRunningFromChats(agentState.chats);
  renderAgentChatList();
  return payload;
}

function mergeAgentRunningFromChats(chats) {
  if (!Array.isArray(chats)) return;
  chats.forEach((chat) => {
    const chatId = String(chat?.id || '');
    const turnId = String(chat?.running_turn_id || '');
    if (!chatId) return;
    if (chat?.running && turnId) {
      agentState.runningByChat[chatId] = { turnId };
    } else if (agentState.runningByChat[chatId]) {
      delete agentState.runningByChat[chatId];
      invalidateAgentChatView(chatId);
    }
  });
  syncAgentBusy();
  updateAgentChatListRunningIndicator();
  syncAgentRunningChatsRefresh();
}

async function refreshAgentChatsSilently() {
  try {
    await refreshAgentChats();
  } catch {
    // Chat list is secondary to the active answer.
  } finally {
    syncAgentRunningChatsRefresh();
  }
}

function setAgentBodyLoading(text = 'Загрузка чата...') {
  const body = $('agent-body');
  if (body) body.innerHTML = `<div class="agent-empty">${text}</div>`;
}

function setAgentBodyError(message) {
  const body = $('agent-body');
  if (body) {
    body.innerHTML = '';
    appendAgentNotice('error', message || 'Не удалось загрузить чат');
  }
}

async function initializeAgentChats() {
  if (!AGENT_ENABLED) return;
  const requestId = ++agentState.pendingChatRequest;
  agentState.loadingChat = true;
  setAgentBodyLoading('Загрузка чатов...');
  updateAgentControls();
  try {
    let payload = await fetchAgentChats();
    if (requestId !== agentState.pendingChatRequest) return;
    let chats = Array.isArray(payload.chats) ? payload.chats : [];
    if (!chats.length) {
      const created = await createAgentChat();
      chats = created.chat ? [created.chat] : [];
      await refreshAgentChatsSilently();
    } else {
      agentState.chats = chats;
    }
    mergeAgentRunningFromChats(chats);
    const savedChatId = readActiveAgentChatId();
    const active = chats.find((chat) => chat.id === savedChatId) || chats[0];
    if (active) {
      await loadAgentChat(active.id, { keepListOpen: false });
    } else {
      setAgentBodyLoading('Новый чат');
      renderAgentUsage(null);
    }
  } catch (err) {
    setAgentBodyError(err.message || 'Не удалось загрузить чаты агента');
  } finally {
    if (requestId === agentState.pendingChatRequest) {
      agentState.loadingChat = false;
      updateAgentControls();
    }
  }
}

async function startNewAgentChat() {
  if (!AGENT_ENABLED || agentState.loadingChat) return;
  detachAgentTurnStream();
  const requestId = ++agentState.pendingChatRequest;
  agentState.loadingChat = true;
  setAgentChatListOpen(false);
  setAgentBodyLoading('Создание чата...');
  updateAgentControls();
  try {
    const payload = await createAgentChat();
    if (requestId !== agentState.pendingChatRequest) return;
    const chat = payload.chat;
    if (!chat?.id) throw new Error('Backend не вернул chat id');
    agentState.chats = [chat, ...agentState.chats.filter((item) => item.id !== chat.id)];
    activateAgentChat(chat.id);
    setAgentLlmProfile(chat.llm_profile_id || agentState.llmProfileId, { skipUsage: true });
    agentState.messages = [];
    agentState.usage = null;
    removeAgentTranscript(chat.id);
    removeAgentUsage(chat.id);
    const body = $('agent-body');
    if (body) body.innerHTML = '<div class="agent-empty">Новый чат</div>';
    renderAgentUsage(null);
    renderAgentChatList();
    const input = $('agent-input');
    if (input) input.focus();
    await refreshAgentChatsSilently();
  } catch (err) {
    setAgentBodyError(err.message || 'Не удалось создать чат');
  } finally {
    if (requestId === agentState.pendingChatRequest) {
      agentState.loadingChat = false;
      updateAgentControls();
    }
  }
}

async function loadAgentChat(chatId, options = {}) {
  if (!AGENT_ENABLED || !chatId) return;
  detachAgentTurnStream();
  const requestId = ++agentState.pendingChatRequest;
  if (!options.keepListOpen) setAgentChatListOpen(false);
  activateAgentChat(chatId);
  renderAgentChatList();
  const restoredFromMemory = restoreAgentCachedView(chatId);
  if (restoredFromMemory) {
    agentState.loadingChat = false;
    updateAgentControls();
    const running = activeAgentRunningTurn();
    if (running?.turnId) {
      subscribeAgentTurnStream(chatId, running.turnId, agentState.lastEventSeqByTurn[running.turnId] || 0);
    }
    refreshAgentChatsSilently();
    return;
  }

  agentState.loadingChat = true;
  if (!restoreAgentTranscript()) {
    setAgentBodyLoading();
  }
  updateAgentControls();
  try {
    const detail = await fetchAgentChatDetail(chatId);
    if (requestId !== agentState.pendingChatRequest) return;
    activateAgentChat(detail.chat?.id || chatId);
    agentState.chats = [
      detail.chat,
      ...agentState.chats.filter((chat) => chat.id !== detail.chat?.id),
    ].filter(Boolean);
    renderAgentChatDetail(detail);
    renderAgentChatList();
  } catch (err) {
    setAgentBodyError(err.message || 'Не удалось загрузить чат');
  } finally {
    if (requestId === agentState.pendingChatRequest) {
      agentState.loadingChat = false;
      updateAgentControls();
    }
  }
}

async function handleAgentChatPopoverClick(event) {
  const deleteButton = event.target.closest('[data-chat-delete]');
  if (deleteButton) {
    event.preventDefault();
    event.stopPropagation();
    const chatId = deleteButton.dataset.chatDelete;
    if (!chatId || agentState.loadingChat) return;
    await removeAgentChat(chatId);
    return;
  }

  const selectButton = event.target.closest('[data-chat-select]');
  if (selectButton) {
    event.preventDefault();
    event.stopPropagation();
    const chatId = selectButton.dataset.chatSelect;
    if (!chatId || chatId === agentState.chatId || agentState.loadingChat) return;
    await loadAgentChat(chatId);
  }
}

async function removeAgentChat(chatId) {
  if (!AGENT_ENABLED || !chatId) return;
  if (agentState.runningByChat[chatId]) {
    appendAgentNotice('warning', 'Нельзя удалить чат, пока агент выполняет задачу.');
    return;
  }
  detachAgentTurnStream();
  const wasActive = chatId === agentState.chatId;
  agentState.loadingChat = true;
  updateAgentControls();
  try {
    await deleteAgentChat(chatId);
    removeAgentTranscript(chatId);
    removeAgentUsage(chatId);
    agentState.chats = agentState.chats.filter((chat) => chat.id !== chatId);
    if (wasActive) {
      const next = agentState.chats[0];
      agentState.loadingChat = false;
      if (next) {
        await loadAgentChat(next.id, { keepListOpen: true });
      } else {
        await startNewAgentChat();
      }
    } else {
      renderAgentChatList();
    }
  } catch (err) {
    appendAgentNotice('error', err.message || 'Не удалось удалить чат');
  } finally {
    agentState.loadingChat = false;
    updateAgentControls();
  }
}

function renderAgentChatDetail(detail) {
  const body = $('agent-body');
  if (!body) return;
  const requestedProfileId = String(detail.chat?.llm_profile_id || '');
  const selectedProfileId = setAgentLlmProfile(requestedProfileId || agentState.llmProfileId, { skipUsage: true });
  body.innerHTML = '';
  agentState.messages = [];
  const turns = Array.isArray(detail.turns) ? detail.turns : [];
  const runningTurn = [...turns].reverse().find((turn) => turn?.status === 'running')
    || (detail.chat?.running && detail.chat?.running_turn_id ? { id: detail.chat.running_turn_id, last_event_seq: 0 } : null);
  if (runningTurn?.id) {
    setAgentRunning(detail.chat?.id || agentState.chatId, runningTurn.id);
  } else {
    clearAgentRunning(detail.chat?.id || agentState.chatId);
  }
  if (!turns.length) {
    body.innerHTML = '<div class="agent-empty">Новый чат</div>';
  } else {
    turns.forEach((turn) => renderAgentTurnFromHistory(turn));
  }
  if (runningTurn?.id) {
    const afterSeq = Number(runningTurn.last_event_seq || runningTurn.event_count || 0);
    agentState.lastEventSeqByTurn[runningTurn.id] = afterSeq;
    subscribeAgentTurnStream(detail.chat?.id || agentState.chatId, runningTurn.id, afterSeq);
  }
  agentState.usage = buildAgentUsageFromChatDetail(detail, turns);
  renderAgentUsage(agentState.usage);
  if (requestedProfileId && selectedProfileId !== requestedProfileId && agentState.llmProfiles.length) {
    appendAgentNotice('warning', 'Профиль модели этого чата больше не доступен. Выбран профиль по умолчанию.');
  }
  renderAgentMermaid(body);
  scrollAgentToBottom(true);
}

function latestAgentTurnUsage(turns) {
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const usage = turns[index]?.usage;
    if (usage && typeof usage === 'object' && Object.keys(usage).length) return usage;
  }
  return null;
}

function buildAgentUsageFromChatDetail(detail, turns) {
  const chatId = detail.chat?.id || agentState.chatId || agentState.sessionId;
  const cachedUsage = readAgentUsage(chatId);
  const turnUsage = latestAgentTurnUsage(turns);
  const latestTurn = [...(Array.isArray(turns) ? turns : [])]
    .reverse()
    .find((turn) => turn?.llm_model || turn?.llm_profile_id || turn?.llm_endpoint_id);
  const sessionUsage = detail.usage && typeof detail.usage === 'object' && Object.keys(detail.usage).length
    ? detail.usage
    : null;
  const base = turnUsage || cachedUsage || {};
  if (!sessionUsage && !base.session && !base.context && !base.turn) return null;
  const profile = currentAgentLlmProfile();
  const llmProfile = base.llm_profile || cachedUsage?.llm_profile || {
    profile_id: latestTurn?.llm_profile_id || detail.chat?.llm_profile_id || profile?.id || '',
    profile_title: profile?.title || '',
    endpoint_id: latestTurn?.llm_endpoint_id || profile?.endpointId || '',
    endpoint_title: profile?.endpointTitle || '',
    model: latestTurn?.llm_model || profile?.model || '',
  };
  return {
    ...base,
    session: sessionUsage || base.session || cachedUsage?.session || {},
    model: base.model || cachedUsage?.model || latestTurn?.llm_model || profile?.model || '',
    llm_profile: llmProfile,
    context: base.context || cachedUsage?.context || {},
    turn: base.turn || cachedUsage?.turn,
  };
}

function renderAgentTurnFromHistory(turn) {
  const userText = String(turn.user_text || '');
  const assistantText = String(turn.assistant_text || '');
  appendAgentMessage('user', userText);
  agentState.messages.push({ role: 'user', text: userText });

  const assistant = appendAgentMessage('assistant', '');
  agentState.activeAssistantMessageEl = assistant || null;
  agentState.activeAssistantTextEl = assistant?.querySelector('.agent-message-text') || null;
  agentState.activeAssistantReasoningEl = assistant?.querySelector('.agent-reasoning') || null;
  agentState.activeAssistantPlanEl = assistant?.querySelector('.agent-plan') || null;
  agentState.activeAssistantStepsEl = assistant?.querySelector('.agent-tool-steps') || null;
  agentState.pendingToolIds = [];
  agentState.toolSeq = 0;
  agentState.activeAnswerRaw = '';
  agentState.activeAnswer = '';
  agentState.activeReasoning = '';

  const replayEvents = Array.isArray(turn.events) ? turn.events : [];
  if (replayEvents.length) {
    replayEvents.forEach((item) => handleAgentEvent(item.event_name || item.event, item.payload || {}));
  } else {
    const reasoningText = String(turn.reasoning_text || '');
    if (reasoningText) setActiveReasoningText(reasoningText);

    const planEvents = Array.isArray(turn.plan?.events) ? turn.plan.events : [];
    planEvents.forEach((item) => handleAgentEvent(item.event, item.payload || {}));

    const toolEvents = Array.isArray(turn.tool_events) ? turn.tool_events : [];
    toolEvents.forEach((item) => handleAgentEvent(item.event, item.payload || {}));

    if (assistantText) {
      agentState.activeAnswerRaw = assistantText;
      setActiveAssistantText(assistantText);
    }
  }
  if (turn.status === 'running') {
    if (assistant) assistant.classList.add('is-streaming');
  } else {
    finishActiveAssistant();
  }

  const notices = Array.isArray(turn.notices) ? turn.notices : [];
  notices.forEach((notice) => appendAgentNotice(notice.kind || 'warning', notice.message || ''));
  const hasErrorNotice = notices.some((notice) => notice.kind === 'error');
  if (turn.status === 'error' && turn.error_message && !hasErrorNotice) {
    appendAgentNotice('error', turn.error_message);
  }
}

function agentContextFromState() {
  const filter = $('analysis-filter');
  const selectedObject = compactSelectedObjectContext(analysisState.selectedObject);
  const selectedNode = compactSelectedNodeContext(analysisState.selectedNode);
  const selectedModule = compactSelectedModuleContext(analysisState.selectedModule);
  return {
    page: getActivePage(),
    selected_kind: analysisState.selectedKind,
    selected_config: analysisState.selectedConfig,
    selected_category: analysisState.selectedCategory,
    selected_ref: analysisState.selectedRef,
    selected_section: analysisState.selectedSection,
    selected_module_id: analysisState.selectedModuleId,
    selected_module_owner: analysisState.selectedModuleOwner,
    selected_module_type: analysisState.selectedModuleType,
    active_tab: analysisState.tab,
    search_query: filter ? filter.value : analysisState.searchQuery,
    current_selection: currentSelection(),
    selected_object: selectedObject,
    selected_node: selectedNode,
    selected_module: selectedModule,
    relationship_groups: compactRelationshipGroups(analysisState.relationships),
  };
}

function compactSelectedObjectContext(data) {
  if (!data) return null;
  const identity = data.identity || {};
  const counters = data.counters || {};
  return dropEmptyFields({
    qualified_name: identity.qualified_name,
    config_name: identity.config_name,
    category: identity.category,
    name: identity.name,
    synonym: identity.synonym,
    comment: identity.comment,
    counters,
    summary_available: Boolean(data.summary),
  });
}

function compactSelectedNodeContext(data) {
  if (!data) return null;
  const node = data.node || {};
  return dropEmptyFields({
    ref: node.ref || node.qualified_name || node.id,
    label: node.label,
    name: node.name,
    qualified_name: node.qualified_name,
    type: node.type,
    module_type: node.module_type,
    owner_qn: node.owner_qn,
  });
}

function compactSelectedModuleContext(data) {
  if (!data) return null;
  const identity = data.identity || {};
  const routines = Array.isArray(data.routines) ? data.routines : [];
  return dropEmptyFields({
    id: identity.id,
    owner_qn: identity.owner_qn,
    owner_name: identity.owner_name,
    module_type: identity.module_type,
    config_name: identity.config_name,
    file_path: identity.file_path,
    routines_count: routines.length,
    exported_routines_count: routines.filter((routine) => routine.export).length,
    routine_names: routines.slice(0, 30).map((routine) => routine.name || routine.signature).filter(Boolean),
    code_available: Boolean(data.code),
  });
}

function compactRelationshipGroups(data) {
  const groups = Array.isArray(data?.groups) ? data.groups : [];
  return groups
    .filter((group) => Array.isArray(group.items) && group.items.length)
    .slice(0, 20)
    .map((group) => ({
      key: group.key,
      title: group.title,
      count: group.items.length,
    }));
}

function dropEmptyFields(value) {
  return Object.fromEntries(Object.entries(value).filter(([, item]) => {
    if (item === undefined || item === null || item === '') return false;
    if (Array.isArray(item) && item.length === 0) return false;
    if (typeof item === 'object' && !Array.isArray(item) && Object.keys(item).length === 0) return false;
    return true;
  }));
}

function agentQuickActionMessage(action) {
  const selection = currentSelection();
  const hasObject = Boolean(analysisState.selectedRef || analysisState.selectedObject);
  const hasModule = Boolean(analysisState.selectedModuleId || analysisState.selectedModule);
  const target = selection?.ref || analysisState.selectedRef || analysisState.selectedModuleOwner || '';

  if (action === 'overview') {
    return target
      ? 'Объясни текущий выбранный объект или элемент: назначение, ключевая структура, формы, BSL и важные связи. Сначала проверь данные через tools.'
      : 'Дай обзор текущего проекта: какие конфигурации и основные категории объектов доступны. Сначала проверь данные через tools.';
  }
  if (action === 'usages') {
    return target
      ? 'Где используется текущий выбранный объект или модуль? Проверь использования, зависимости, пути связей и вызовы, если выбран код.'
      : 'Покажи, какие данные сейчас выбраны на экране, и объясни, для какого объекта или модуля можно проверить использования.';
  }
  if (action === 'access') {
    return 'Проверь права текущего выбранного объекта. Найди роли и права к объекту через tools, сгруппируй результат по ролям и типам прав, укажи важные ограничения.';
  }
  if (action === 'form') {
    return hasObject
      ? 'Разбери формы текущего объекта: список форм, элементы, реквизиты формы, события, обработчики и привязки. Сначала проверь структуру форм через tools.'
      : 'Проверь текущий выбор на экране и определи, какую форму или объект можно разобрать. Если формы нет в текущем выборе, коротко скажи, что надо выбрать.';
  }
  if (action === 'module') {
    return hasModule
      ? 'Разбери текущий модуль: назначение, основные процедуры/функции, экспортные методы, ключевые вызовы и возможные точки входа.'
      : 'Найди и разбери BSL-модули текущего объекта: какие модули есть, основные процедуры/функции и где находится ключевая логика.';
  }
  return '';
}

function clearAgentEmptyState() {
  const body = $('agent-body');
  const empty = body ? body.querySelector('.agent-empty, .agent-disabled') : null;
  if (empty) empty.remove();
}

function appendAgentMessage(role, text = '') {
  const body = $('agent-body');
  if (!body) return null;
  clearAgentEmptyState();
  const node = document.createElement('div');
  node.className = `agent-message agent-message-${role}`;
  if (role === 'assistant' && text) {
    node.setAttribute('data-raw-markdown', String(text));
  }

  if (role === 'assistant') {
    node.classList.add('is-streaming');
    const reasoningEl = document.createElement('details');
    reasoningEl.className = 'agent-reasoning';
    reasoningEl.hidden = true;
    reasoningEl.open = true;

    const reasoningTitle = document.createElement('summary');
    reasoningTitle.className = 'agent-reasoning-title';
    reasoningTitle.textContent = 'Рассуждение';
    reasoningEl.appendChild(reasoningTitle);

    const reasoningBody = document.createElement('div');
    reasoningBody.className = 'agent-reasoning-body';
    reasoningEl.appendChild(reasoningBody);
    node.appendChild(reasoningEl);

    const planEl = document.createElement('details');
    planEl.className = 'agent-plan';
    planEl.hidden = true;
    planEl.open = true;
    node.appendChild(planEl);

    const stepsEl = document.createElement('div');
    stepsEl.className = 'agent-tool-steps';
    node.appendChild(stepsEl);
  }

  const textEl = document.createElement('div');
  textEl.className = 'agent-message-text';
  node.appendChild(textEl);
  renderAgentMarkdown(textEl, text);

  body.appendChild(node);
  scrollAgentToBottom();
  return node;
}

async function copyTextToClipboard(text) {
  const value = String(text || '');
  if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

function setAgentMessageCopyState(button, state) {
  button.classList.toggle('copied', state === 'copied');
  button.classList.toggle('copy-error', state === 'error');
  button.textContent = state === 'copied' ? '✓' : state === 'error' ? '!' : '⧉';
}

function ensureAgentMessageCopyButton(message) {
  if (!message || message.querySelector('.agent-message-copy')) return;
  const raw = message.getAttribute('data-raw-markdown') || '';
  if (!raw.trim()) return;

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'agent-message-copy';
  button.title = 'Скопировать ответ в Markdown';
  button.setAttribute('aria-label', 'Скопировать ответ в Markdown');
  button.textContent = '⧉';
  message.appendChild(button);
}

async function handleAgentMessageCopyClick(event) {
  const button = event.target.closest('.agent-message-copy');
  if (!button) return;
  const message = button.closest('.agent-message-assistant');
  const raw = message?.getAttribute('data-raw-markdown') || '';
  if (!raw.trim()) return;
  event.preventDefault();

  try {
    await copyTextToClipboard(raw);
    setAgentMessageCopyState(button, 'copied');
  } catch {
    setAgentMessageCopyState(button, 'error');
  }
  window.setTimeout(() => setAgentMessageCopyState(button, 'idle'), 1600);
}

function createAgentToolOpenButton() {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'agent-tool-open';
  button.title = 'Открыть запрос и ответ в большом окне';
  button.setAttribute('aria-label', 'Открыть запрос и ответ инструмента');
  button.textContent = '⛶';
  return button;
}

function handleAgentToolOpenClick(event) {
  const button = event.target.closest('.agent-tool-open');
  if (!button) return;
  const block = button.closest('.agent-tool-row');
  if (!block) return;
  event.preventDefault();
  event.stopPropagation();
  openAgentToolModal(block);
}

function ensureAgentToolModal() {
  let modal = $('agent-tool-modal');
  if (modal) return modal;

  modal = document.createElement('div');
  modal.id = 'agent-tool-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="agent-tool-modal-close"></div>
    <section class="condition-modal-dialog agent-tool-dialog" role="dialog" aria-modal="true"
             aria-labelledby="agent-tool-modal-title">
      <header class="condition-modal-header agent-tool-modal-header" data-role="agent-tool-modal-drag">
        <div>
          <h3 id="agent-tool-modal-title"></h3>
          <p data-role="agent-tool-modal-meta"></p>
        </div>
        <button class="panel-icon-button" type="button" data-role="agent-tool-modal-close" title="Закрыть">×</button>
      </header>
      <div class="agent-tool-modal-body" data-role="agent-tool-modal-body"></div>
    </section>
  `;
  modal.addEventListener('click', async (event) => {
    if (event.target.closest('[data-role="agent-tool-modal-close"]')) {
      closeAgentToolModal();
      return;
    }
    const copyButton = event.target.closest('[data-role="agent-tool-modal-copy"]');
    if (copyButton) await copyAgentToolModalBlock(copyButton);
  });
  modal.addEventListener('pointerdown', startAgentToolModalDrag);
  modal.addEventListener('pointermove', moveAgentToolModalDrag);
  modal.addEventListener('pointerup', stopAgentToolModalDrag);
  if (!window.__agentToolModalEscBound) {
    window.__agentToolModalEscBound = true;
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !$('agent-tool-modal')?.hidden) closeAgentToolModal();
    });
  }
  document.body.appendChild(modal);
  return modal;
}

function openAgentToolModal(block) {
  const modal = ensureAgentToolModal();
  const name = block.dataset.toolName || block.querySelector('.agent-tool-title')?.textContent || 'tool';
  const server = block.dataset.toolServer || '';
  const args = block.dataset.argumentsPreview || '';
  const result = block.dataset.resultPreview || '';

  modal.querySelector('#agent-tool-modal-title').textContent = name;
  modal.querySelector('[data-role="agent-tool-modal-meta"]').textContent = server || 'MCP tool';
  modal.querySelector('[data-role="agent-tool-modal-body"]').innerHTML = [
    agentToolModalCodeBlock('Запрос', args, 'request'),
    agentToolModalCodeBlock('Ответ', result, 'response'),
  ].join('');
  modal.hidden = false;
  positionAgentToolModal();
  modal.querySelector('button[data-role="agent-tool-modal-close"]')?.focus();
}

function closeAgentToolModal() {
  const modal = $('agent-tool-modal');
  if (modal) modal.hidden = true;
  agentToolModalState.drag = null;
}

function getAgentToolDialog() {
  return $('agent-tool-modal')?.querySelector('.agent-tool-dialog') || null;
}

function positionAgentToolModal() {
  const dialog = getAgentToolDialog();
  if (!dialog) return;
  const frame = clampAgentToolModalFrame(agentToolModalState.frame || defaultAgentToolModalFrame());
  agentToolModalState.frame = frame;
  dialog.style.left = `${frame.left}px`;
  dialog.style.top = `${frame.top}px`;
  dialog.style.width = `${frame.width}px`;
  dialog.style.height = `${frame.height}px`;
}

function defaultAgentToolModalFrame() {
  const width = Math.min(1180, Math.round(window.innerWidth * 0.92));
  const height = Math.min(820, Math.round(window.innerHeight * 0.86));
  return {
    left: Math.max(12, Math.round((window.innerWidth - width) / 2)),
    top: Math.max(12, Math.round((window.innerHeight - height) / 2)),
    width,
    height,
  };
}

function clampAgentToolModalFrame(frame) {
  const width = Math.min(Math.max(520, Math.round(frame.width || 0)), Math.max(520, window.innerWidth - 16));
  const height = Math.min(Math.max(360, Math.round(frame.height || 0)), Math.max(360, window.innerHeight - 16));
  return {
    left: Math.min(Math.max(8, Math.round(frame.left || 8)), Math.max(8, window.innerWidth - width - 8)),
    top: Math.min(Math.max(8, Math.round(frame.top || 8)), Math.max(8, window.innerHeight - 80)),
    width,
    height,
  };
}

function rememberAgentToolModalFrame() {
  const dialog = getAgentToolDialog();
  if (!dialog) return;
  const rect = dialog.getBoundingClientRect();
  agentToolModalState.frame = clampAgentToolModalFrame({
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  });
}

function startAgentToolModalDrag(event) {
  const handle = event.target.closest('[data-role="agent-tool-modal-drag"]');
  if (!handle || event.target.closest('button')) return;
  const dialog = getAgentToolDialog();
  if (!dialog) return;
  const rect = dialog.getBoundingClientRect();
  agentToolModalState.drag = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
  };
  dialog.setPointerCapture?.(event.pointerId);
  event.preventDefault();
}

function moveAgentToolModalDrag(event) {
  const drag = agentToolModalState.drag;
  if (!drag || drag.pointerId !== event.pointerId) return;
  const dialog = getAgentToolDialog();
  if (!dialog) return;
  const frame = clampAgentToolModalFrame({
    left: drag.left + event.clientX - drag.startX,
    top: drag.top + event.clientY - drag.startY,
    width: drag.width,
    height: drag.height,
  });
  dialog.style.left = `${frame.left}px`;
  dialog.style.top = `${frame.top}px`;
}

function stopAgentToolModalDrag(event) {
  const drag = agentToolModalState.drag;
  if (drag && drag.pointerId !== event.pointerId) return;
  rememberAgentToolModalFrame();
  agentToolModalState.drag = null;
}

function countTextCharacters(text) {
  return Array.from(String(text || '')).length;
}

function estimateTextTokens(text) {
  const length = countTextCharacters(text);
  return length ? Math.max(1, Math.ceil(length / 3)) : 0;
}

function agentToolModalTextStats(text) {
  const chars = countTextCharacters(text);
  const tokens = estimateTextTokens(text);
  return `${formatRuNumber(chars)} симв. · ≈${formatRuNumber(tokens)} ток.`;
}

function agentToolModalCodeBlock(title, text, kind) {
  const value = String(text || '').trim();
  const typeClass = kind === 'response' ? 'agent-tool-modal-code-response' : 'agent-tool-modal-code-request';
  return `
    <section class="agent-tool-modal-code ${typeClass}">
      <div class="agent-tool-modal-code-header">
        <div class="agent-tool-modal-code-title">
          <h4>${escapeHtml(title)}</h4>
          <span>${escapeHtml(agentToolModalTextStats(value))}</span>
        </div>
        <button class="agent-tool-modal-copy" type="button" data-role="agent-tool-modal-copy">Скопировать</button>
      </div>
      <pre><code>${escapeHtml(value || '—')}</code></pre>
    </section>
  `;
}

async function copyAgentToolModalBlock(button) {
  const block = button.closest('.agent-tool-modal-code');
  const text = block?.querySelector('pre')?.textContent || '';
  try {
    await copyTextToClipboard(text);
    button.textContent = 'Скопировано';
    button.classList.add('copied');
  } catch {
    button.textContent = 'Ошибка';
    button.classList.add('copy-error');
  }
  window.setTimeout(() => {
    button.textContent = 'Скопировать';
    button.classList.remove('copied', 'copy-error');
  }, 1600);
}

function decodeConsoleLinkParam(params, ...names) {
  for (const name of names) {
    const value = params.get(name);
    if (value !== null && value !== '') return value;
  }
  return '';
}

function inferConsoleLinkKind(ref) {
  const value = String(ref || '');
  if (/\/(?:Attribute|StandardAttribute|TabularPart|Form|Command|Event|Predefined|EnumValue|Dimension|Resource|Requisite)\//i.test(value)) {
    return 'node';
  }
  return 'object';
}

async function handleAgentConsoleLinkClick(event) {
  const link = event.target.closest('a[data-console-link], a[href^="metacode://"]');
  if (!link) return;
  event.preventDefault();
  event.stopPropagation();

  let url;
  try {
    url = new URL(link.getAttribute('href') || '');
  } catch {
    appendAgentNotice('error', 'Некорректная ссылка агента');
    return;
  }
  if (url.protocol !== 'metacode:') return;

  const params = url.searchParams;
  const ref = decodeConsoleLinkParam(params, 'ref', 'qn', 'qualified_name', 'object_ref');
  const section = decodeConsoleLinkParam(params, 'section');
  const tab = decodeConsoleLinkParam(params, 'tab');
  const moduleId = decodeConsoleLinkParam(params, 'module_id', 'moduleId');
  const ownerRef = decodeConsoleLinkParam(params, 'owner_ref', 'ownerRef', 'owner_qn');
  const moduleType = decodeConsoleLinkParam(params, 'module_type', 'moduleType', 'type');
  const kind = (decodeConsoleLinkParam(params, 'kind') || url.hostname || inferConsoleLinkKind(ref)).toLowerCase();

  try {
    if (kind === 'module') {
      await selectModule({ moduleId, ownerRef, moduleType, revealInTree: true });
      revealActiveTreeNode();
      return;
    }
    if (kind === 'section') {
      await selectObjectSection(ref, section, { revealInTree: true });
      revealActiveTreeNode();
      return;
    }
    if (kind === 'node' || kind === 'element') {
      await selectNode(ref, '', { revealInTree: true });
      revealActiveTreeNode();
      return;
    }
    if (kind === 'object' || kind === 'open') {
      await selectObject(ref, { tab: tab ? normalizeObjectTab(tab) : analysisState.tab, revealInTree: true });
      revealActiveTreeNode();
      return;
    }
    appendAgentNotice('error', `Неизвестный тип ссылки: ${kind}`);
  } catch (err) {
    appendAgentNotice('error', err.message || 'Не удалось открыть ссылку агента');
  }
}

function appendAgentNotice(kind, message) {
  const body = $('agent-body');
  if (!body) return;
  const text = String(message || '').trim();
  if (!text) return;
  const previous = body.lastElementChild;
  if (
    previous
    && previous.classList.contains('agent-notice')
    && previous.dataset.kind === kind
    && previous.dataset.message === text
  ) {
    scrollAgentToBottom();
    return;
  }
  clearAgentEmptyState();
  const node = document.createElement('div');
  node.className = `agent-notice agent-notice-${kind}`;
  node.dataset.kind = kind;
  node.dataset.message = text;
  node.textContent = text;
  body.appendChild(node);
  scrollAgentToBottom();
}

function initAgentMermaid() {
  if (!window.mermaid || window.__consoleAgentMermaidInitialized) return;
  try {
    window.mermaid.initialize({
      startOnLoad: false,
      theme: 'dark',
      themeVariables: {
        darkMode: true,
        background: '#101827',
        primaryColor: '#38bdf8',
        primaryTextColor: '#e5eefb',
        primaryBorderColor: '#475569',
        lineColor: '#94a3b8',
        secondaryColor: '#1e293b',
        tertiaryColor: '#0f172a',
      },
      suppressErrorRendering: true,
      logLevel: 'fatal',
    });
    window.__consoleAgentMermaidInitialized = true;
  } catch {
    // Mermaid is best-effort for agent answers.
  }
}

function renderAgentMermaid(container) {
  if (!window.mermaid) return;
  initAgentMermaid();
  container.querySelectorAll('.mermaid:not([data-processed])').forEach(async (node) => {
    const id = `agent-mermaid-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const raw = node.getAttribute('data-mermaid-code') || node.textContent || '';
    const textarea = document.createElement('textarea');
    textarea.innerHTML = raw;
    const code = textarea.value.trim();
    try {
      const { svg } = await window.mermaid.render(id, code);
      node.innerHTML = svg;
      node.dataset.processed = 'true';
    } catch (err) {
      node.dataset.processed = 'true';
      node.dataset.error = 'true';
      node.textContent = `Ошибка Mermaid: ${err.message || err}`;
    }
  });
}

function setupMermaidFullscreenModal() {
  if (window.__consoleMermaidFullscreenBound) return;
  window.__consoleMermaidFullscreenBound = true;

  let mermaidModal = null;
  let modalZoom = 1;

  function createMermaidModal() {
    if (mermaidModal) return mermaidModal;

    const modal = document.createElement('div');
    modal.id = 'mermaid-fullscreen-modal';
    modal.style.cssText = `
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0, 0, 0, 0.95);
      z-index: 10000;
      overflow: auto;
    `;

    const content = document.createElement('div');
    content.className = 'mermaid-fullscreen-content';
    content.style.cssText = `
      position: relative;
      width: 100%;
      height: 100%;
      padding: 60px 20px 20px;
      box-sizing: border-box;
      overflow: auto;
    `;

    const centerWrapper = document.createElement('div');
    centerWrapper.className = 'mermaid-center-wrapper';
    centerWrapper.style.cssText = `
      min-height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
    `;

    const diagramWrapper = document.createElement('div');
    diagramWrapper.className = 'mermaid-fullscreen-wrapper';
    diagramWrapper.style.cssText = `
      position: relative;
      display: inline-block;
    `;

    const diagramContainer = document.createElement('div');
    diagramContainer.className = 'mermaid-fullscreen-diagram';
    diagramContainer.style.cssText = `
      transform-origin: center center;
      transition: transform 0.2s ease;
    `;

    const controls = document.createElement('div');
    controls.className = 'mermaid-fullscreen-controls';
    controls.style.cssText = `
      position: fixed;
      top: 10px;
      right: 10px;
      display: flex;
      gap: 8px;
      z-index: 10001;
    `;

    const buttonStyle = `
      padding: 8px 16px;
      border-radius: 8px;
      border: 1px solid rgba(255, 255, 255, 0.3);
      background: rgba(255, 255, 255, 0.1);
      color: #fff;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
      transition: all 0.2s ease;
    `;

    const zoomOutBtn = document.createElement('button');
    zoomOutBtn.textContent = '−';
    zoomOutBtn.title = 'Уменьшить (Ctrl + колесико вниз)';
    zoomOutBtn.style.cssText = buttonStyle;

    const zoomInBtn = document.createElement('button');
    zoomInBtn.textContent = '+';
    zoomInBtn.title = 'Увеличить (Ctrl + колесико вверх)';
    zoomInBtn.style.cssText = buttonStyle;

    const resetBtn = document.createElement('button');
    resetBtn.textContent = '100%';
    resetBtn.title = 'Сбросить масштаб';
    resetBtn.style.cssText = buttonStyle;

    const closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.title = 'Закрыть (ESC)';
    closeBtn.style.cssText = buttonStyle;

    controls.appendChild(zoomOutBtn);
    controls.appendChild(zoomInBtn);
    controls.appendChild(resetBtn);
    controls.appendChild(closeBtn);

    diagramWrapper.appendChild(diagramContainer);
    centerWrapper.appendChild(diagramWrapper);
    content.appendChild(centerWrapper);
    modal.appendChild(controls);
    modal.appendChild(content);
    document.body.appendChild(modal);

    let naturalSvgWidth = 0;
    let naturalSvgHeight = 0;

    function updateZoom(newZoom) {
      modalZoom = Math.max(0.1, Math.min(100, newZoom));
      diagramContainer.style.transform = `scale(${modalZoom})`;
      resetBtn.textContent = `${Math.round(modalZoom * 100)}%`;

      if (naturalSvgWidth && naturalSvgHeight) {
        diagramWrapper.style.width = `${naturalSvgWidth}px`;
        diagramWrapper.style.height = `${naturalSvgHeight}px`;

        const scaledWidth = naturalSvgWidth * modalZoom;
        const scaledHeight = naturalSvgHeight * modalZoom;
        centerWrapper.style.minWidth = `${scaledWidth}px`;
        centerWrapper.style.minHeight = `${scaledHeight}px`;
      }
    }

    function closeModal() {
      modal.style.display = 'none';
      modalZoom = 1;
      updateZoom(1);
    }

    zoomInBtn.addEventListener('click', () => updateZoom(modalZoom + 0.25));
    zoomOutBtn.addEventListener('click', () => updateZoom(modalZoom - 0.25));
    resetBtn.addEventListener('click', () => updateZoom(1));
    closeBtn.addEventListener('click', closeModal);

    modal.addEventListener('click', (event) => {
      if (event.target === modal || event.target === content) {
        closeModal();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && modal.style.display === 'flex') {
        closeModal();
      }
    });

    diagramContainer.addEventListener('wheel', (event) => {
      if (event.ctrlKey || event.metaKey) {
        event.preventDefault();
        const delta = event.deltaY > 0 ? -0.1 : 0.1;
        updateZoom(modalZoom + delta);
      }
    }, { passive: false });

    mermaidModal = {
      element: modal,
      diagramContainer,
      show: (svgContent) => {
        diagramContainer.innerHTML = svgContent;
        diagramContainer.style.transform = 'scale(1)';
        modal.style.display = 'flex';

        const svg = diagramContainer.querySelector('svg');
        if (svg) {
          void svg.offsetWidth;

          const svgRect = svg.getBoundingClientRect();
          naturalSvgWidth = svgRect.width || 800;
          naturalSvgHeight = svgRect.height || 600;

          const availableWidth = window.innerWidth * 0.90;
          const availableHeight = window.innerHeight * 0.85;
          const scaleX = availableWidth / naturalSvgWidth;
          const scaleY = availableHeight / naturalSvgHeight;
          const optimalScale = Math.min(scaleX, scaleY);
          const finalScale = Math.max(optimalScale, 1);

          modalZoom = finalScale;
          updateZoom(finalScale);
        } else {
          naturalSvgWidth = 0;
          naturalSvgHeight = 0;
          modalZoom = 1;
          updateZoom(1);
        }
      },
    };

    return mermaidModal;
  }

  document.addEventListener('mermaid-fullscreen', (event) => {
    const modal = createMermaidModal();
    modal.show(event.detail.svg);
  });
}

function renderAgentMarkdown(target, text) {
  const value = String(text || '');
  if (window.Markdown && typeof window.Markdown.render === 'function') {
    target.innerHTML = window.Markdown.render(value);
    if (window.BSL && typeof window.BSL.highlightAll === 'function') {
      try {
        window.BSL.highlightAll(target, { autodetect: true, inline: false });
      } catch {
        // Highlighting should not break chat rendering.
      }
    }
    if (window.XML && typeof window.XML.highlightAll === 'function') {
      try {
        window.XML.highlightAll(target, { autodetect: true, inline: false });
      } catch {
        // Highlighting should not break chat rendering.
      }
    }
    renderAgentMermaid(target);
  } else {
    target.textContent = value;
  }
}

function renderAgentToolPreview(target, value) {
  target.textContent = String(value || '').trim();
}

function resizeAgentInput() {
  const input = $('agent-input');
  if (!input) return;
  input.style.height = 'auto';
  const style = getComputedStyle(input);
  const lineHeight = parseFloat(style.lineHeight) || 18;
  const border = parseFloat(style.borderTopWidth || 0) + parseFloat(style.borderBottomWidth || 0);
  const padding = parseFloat(style.paddingTop || 0) + parseFloat(style.paddingBottom || 0);
  const maxHeight = Math.ceil((lineHeight * 5) + padding + border);
  input.style.height = `${Math.min(input.scrollHeight, maxHeight)}px`;
  input.style.overflowY = input.scrollHeight > maxHeight ? 'auto' : 'hidden';
}

async function stopAgentRun() {
  const running = activeAgentRunningTurn();
  if (!running || !agentState.chatId) return;
  try {
    const payload = await stopAgentTurn(agentState.chatId, running.turnId);
    handleAgentEvent('turn_status', {
      chat_id: agentState.chatId,
      turn_id: running.turnId,
      status: payload.turn?.status || 'stopped',
      message: '',
    });
  } catch (err) {
    appendAgentNotice('error', err.message || 'Не удалось остановить агента');
  }
}

class AgentCodeFormatMenu {
  constructor(textarea, menu) {
    this.textarea = textarea;
    this.menu = menu;
    this.selectedText = '';
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.isOpen = false;
    if (this.textarea && this.menu) this.init();
  }

  init() {
    this.textarea.addEventListener('paste', (event) => {
      const pastedText = event.clipboardData?.getData('text') || '';
      if (!pastedText.trim() || !/[\r\n]/.test(pastedText)) return;
      const startBeforePaste = this.textarea.selectionStart;
      window.setTimeout(() => {
        this.selectionStart = startBeforePaste;
        this.selectionEnd = startBeforePaste + pastedText.length;
        this.selectedText = pastedText;
        this.textarea.setSelectionRange(this.selectionStart, this.selectionEnd);
        this.show();
      }, 10);
    });

    this.textarea.addEventListener('mouseup', () => this.handleSelection());
    this.textarea.addEventListener('keyup', () => this.handleSelection());
    this.textarea.addEventListener('mousedown', () => this.hide());
    this.textarea.addEventListener('keydown', (event) => this.handleHotkey(event));

    this.menu.querySelectorAll('.agent-format-btn').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.preventDefault();
        this.applyFormat(event.currentTarget.dataset.format || 'bsl');
      });
    });

    document.addEventListener('click', (event) => {
      if (!this.isOpen) return;
      if (this.menu.contains(event.target) || event.target === this.textarea) return;
      this.hide();
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') this.hide();
    });
  }

  handleSelection() {
    const start = this.textarea.selectionStart;
    const end = this.textarea.selectionEnd;
    if (start === end) {
      this.hide();
      return;
    }
    const selectedText = this.textarea.value.slice(start, end);
    if (selectedText.trim().length < 2) {
      this.hide();
      return;
    }
    this.selectionStart = start;
    this.selectionEnd = end;
    this.selectedText = selectedText;
    this.show();
  }

  handleHotkey(event) {
    if (!event.ctrlKey || !event.shiftKey) return;
    const key = String(event.key || '').toLowerCase();
    const lang = key === 'x' ? 'xml' : (key === '!' || key === '1') ? 'bsl' : '';
    if (!lang || this.textarea.selectionStart === this.textarea.selectionEnd) return;
    event.preventDefault();
    this.selectionStart = this.textarea.selectionStart;
    this.selectionEnd = this.textarea.selectionEnd;
    this.selectedText = this.textarea.value.slice(this.selectionStart, this.selectionEnd);
    this.applyFormat(lang);
  }

  detectLanguage(code) {
    const text = String(code || '').trim();
    if (/^\s*<\?xml|^\s*<[a-zA-Z][\w\-:]*[\s>]/.test(text)) return 'xml';
    if (/\b(Функция|Процедура|КонецФункции|КонецПроцедуры|Если|Тогда|Иначе|Для|Каждого|Из|Цикл|Возврат|Function|Procedure|EndFunction|EndProcedure)\b/i.test(text)) {
      return 'bsl';
    }
    return 'bsl';
  }

  show() {
    const rect = this.textarea.getBoundingClientRect();
    const width = 112;
    this.menu.style.top = `${Math.max(8, rect.top - 4)}px`;
    this.menu.style.left = `${Math.max(8, rect.right - width)}px`;
    const detected = this.detectLanguage(this.selectedText);
    this.menu.querySelectorAll('.agent-format-btn').forEach((button) => {
      button.classList.toggle('is-suggested', button.dataset.format === detected);
    });
    this.menu.classList.remove('hidden');
    this.isOpen = true;
  }

  hide() {
    if (!this.menu) return;
    this.menu.classList.add('hidden');
    this.isOpen = false;
  }

  applyFormat(lang) {
    if (!this.selectedText) return;
    const cleanLang = lang === 'xml' ? 'xml' : 'bsl';
    const value = this.textarea.value;
    const selected = this.selectedText.replace(/^\s*\n/, '').replace(/\n\s*$/, '');
    const formatted = `\`\`\`${cleanLang}\n${selected}\n\`\`\``;
    this.textarea.value = value.slice(0, this.selectionStart) + formatted + value.slice(this.selectionEnd);
    const cursor = this.selectionStart + formatted.length;
    this.textarea.setSelectionRange(cursor, cursor);
    this.textarea.focus();
    this.hide();
    resizeAgentInput();
  }
}

function appendAgentToolRow(kind, payload) {
  const steps = agentState.activeAssistantStepsEl || $('agent-body');
  if (!steps) return;

  if (kind === 'call') {
    const toolId = `tool-${++agentState.toolSeq}`;
    agentState.pendingToolIds.push(toolId);

    const block = document.createElement('details');
    block.className = 'agent-tool-row agent-tool-call';
    block.dataset.toolId = toolId;
    block.dataset.toolName = payload.name || 'tool';
    block.dataset.toolServer = payload.server || '';
    block.open = true;

    const summary = document.createElement('summary');
    summary.className = 'agent-tool-summary';

    const title = document.createElement('span');
    title.className = 'agent-tool-title';
    title.textContent = payload.name || 'tool';
    summary.appendChild(title);

    const meta = document.createElement('span');
    meta.className = 'agent-tool-meta';
    meta.textContent = payload.server ? `${payload.server} · выполняется` : 'выполняется';
    summary.appendChild(meta);
    summary.appendChild(createAgentToolOpenButton());
    block.appendChild(summary);

    const preview = payload.arguments_preview || '';
    block.dataset.argumentsPreview = preview;
    if (preview) {
      const previewEl = document.createElement('div');
      previewEl.className = 'agent-tool-preview';
      renderAgentToolPreview(previewEl, preview);
      block.appendChild(previewEl);
    }

    steps.appendChild(block);
    scrollAgentToBottom();
    return;
  }

  const toolId = agentState.pendingToolIds.shift() || `tool-${++agentState.toolSeq}`;
  let block = steps.querySelector(`[data-tool-id="${CSS.escape(toolId)}"]`);
  if (!block) {
    block = document.createElement('details');
    block.className = 'agent-tool-row agent-tool-call';
    block.dataset.toolId = toolId;
    steps.appendChild(block);
  }

  block.classList.add(payload.status === 'error' ? 'agent-tool-error' : 'agent-tool-done');
  block.dataset.toolName = payload.name || block.dataset.toolName || 'tool';
  block.dataset.toolServer = payload.server || block.dataset.toolServer || '';
  block.dataset.resultPreview = payload.preview || '';
  const summary = block.querySelector('.agent-tool-summary') || document.createElement('summary');
  summary.className = 'agent-tool-summary';
  if (!summary.parentElement) block.appendChild(summary);

  let title = summary.querySelector('.agent-tool-title');
  if (!title) {
    title = document.createElement('span');
    title.className = 'agent-tool-title';
    summary.appendChild(title);
  }
  title.textContent = payload.name || title.textContent || 'tool';

  let meta = summary.querySelector('.agent-tool-meta');
  if (!meta) {
    meta = document.createElement('span');
    meta.className = 'agent-tool-meta';
    summary.appendChild(meta);
  }
  const status = payload.status === 'error' ? 'ошибка' : 'готово';
  meta.textContent = payload.server ? `${payload.server} · ${status}` : status;
  if (!summary.querySelector('.agent-tool-open')) summary.appendChild(createAgentToolOpenButton());

  const oldResult = block.querySelector('.agent-tool-result-body');
  if (oldResult) oldResult.remove();

  const preview = payload.preview || '';
  if (preview) {
    const resultEl = document.createElement('div');
    resultEl.className = 'agent-tool-preview agent-tool-result-body';
    renderAgentToolPreview(resultEl, preview);
    block.appendChild(resultEl);
  }

  block.open = false;
  scrollAgentToBottom();
}

function ensureAgentPlanEl() {
  if (agentState.activeAssistantPlanEl) return agentState.activeAssistantPlanEl;
  const textEl = agentState.activeAssistantTextEl;
  const message = textEl ? textEl.closest('.agent-message-assistant') : null;
  const planEl = message ? message.querySelector('.agent-plan') : null;
  if (planEl) {
    agentState.activeAssistantPlanEl = planEl;
    return planEl;
  }
  return null;
}

function agentPlanStatusLabel(status) {
  if (status === 'in_progress') return 'В работе';
  if (status === 'done') return 'Готово';
  if (status === 'blocked') return 'Блок';
  return 'Ожидает';
}

function renderAgentPlan(payload) {
  const planEl = ensureAgentPlanEl();
  if (!planEl) return;
  const steps = Array.isArray(payload.steps) ? payload.steps : [];
  if (!steps.length) return;

  planEl.hidden = false;
  planEl.innerHTML = '';
  planEl.open = true;

  const title = document.createElement('summary');
  title.className = 'agent-plan-title';
  title.textContent = 'План действий';
  planEl.appendChild(title);

  const list = document.createElement('ol');
  list.className = 'agent-plan-list';

  steps.forEach((step, index) => {
    const item = document.createElement('li');
    const stepId = String(step.id || index + 1);
    const status = String(step.status || 'pending');
    item.className = `agent-plan-step is-${status}`;
    item.dataset.planStepId = stepId;

    const text = document.createElement('span');
    text.className = 'agent-plan-step-text';
    text.textContent = String(step.title || `Шаг ${stepId}`);
    item.appendChild(text);

    const badge = document.createElement('span');
    badge.className = 'agent-plan-step-status';
    badge.textContent = agentPlanStatusLabel(status);
    item.appendChild(badge);

    if (step.note) {
      const note = document.createElement('div');
      note.className = 'agent-plan-step-note';
      note.textContent = String(step.note);
      item.appendChild(note);
    }

    list.appendChild(item);
  });

  planEl.appendChild(list);
  scrollAgentToBottom();
}

function updateAgentPlanStep(payload) {
  const planEl = ensureAgentPlanEl();
  if (!planEl) return;
  const stepId = String(payload.id || payload.step_id || '');
  if (!stepId) return;
  const item = planEl.querySelector(`[data-plan-step-id="${CSS.escape(stepId)}"]`);
  if (!item) return;

  const status = String(payload.status || 'pending');
  item.className = `agent-plan-step is-${status}`;

  const badge = item.querySelector('.agent-plan-step-status');
  if (badge) badge.textContent = agentPlanStatusLabel(status);

  const noteText = String(payload.note || '').trim();
  let note = item.querySelector('.agent-plan-step-note');
  if (noteText) {
    if (!note) {
      note = document.createElement('div');
      note.className = 'agent-plan-step-note';
      item.appendChild(note);
    }
    note.textContent = noteText;
  } else if (note) {
    note.remove();
  }

  scrollAgentToBottom();
}

function completeAgentPlan(payload) {
  const planEl = ensureAgentPlanEl();
  if (!planEl) return;
  planEl.classList.add('is-complete');
  if (Array.isArray(payload.steps)) {
    payload.steps.forEach((step) => updateAgentPlanStep(step));
  }
  const summaryText = String(payload.summary || '').trim();
  if (summaryText) {
    let summary = planEl.querySelector('.agent-plan-summary');
    if (!summary) {
      summary = document.createElement('div');
      summary.className = 'agent-plan-summary';
      planEl.appendChild(summary);
    }
    summary.textContent = summaryText;
  }
  planEl.open = false;
  scrollAgentToBottom();
}

function isAgentBodyNearBottom(body = $('agent-body')) {
  if (!body) return true;
  const distance = body.scrollHeight - body.scrollTop - body.clientHeight;
  return distance < 56;
}

function scrollAgentToBottom(force = false) {
  const body = $('agent-body');
  if (body && (force || agentState.autoScroll)) {
    body.scrollTop = body.scrollHeight;
    agentState.autoScroll = true;
  }
  scheduleAgentTranscriptPersist();
}

function stripStreamingToolMarkup(text) {
  let value = String(text || '');
  const markers = [
    '<｜DSML｜tool_calls>',
    '<|DSML|tool_calls>',
    '<｜tool_calls｜>',
    '<|tool_calls|>',
  ];
  for (const marker of markers) {
    const index = value.indexOf(marker);
    if (index >= 0) value = value.slice(0, index);
  }
  return value
    .replace(/<[\uFF5C|]DSML[\uFF5C|][\s\S]*$/g, '')
    .replace(/<[\uFF5C|]tool_calls[\uFF5C|][\s\S]*$/g, '')
    .replace(/\n{3,}$/g, '\n\n');
}

function setActiveAssistantText(text) {
  agentState.activeAnswer = text;
  if (agentState.activeAssistantTextEl) {
    renderAgentMarkdown(agentState.activeAssistantTextEl, text);
  }
  const message = agentState.activeAssistantMessageEl
    || agentState.activeAssistantTextEl?.closest('.agent-message-assistant');
  if (message) message.setAttribute('data-raw-markdown', String(text || ''));
  scrollAgentToBottom();
}

function setActiveReasoningText(text) {
  agentState.activeReasoning = text;
  const reasoningEl = agentState.activeAssistantReasoningEl;
  if (!reasoningEl) return;
  reasoningEl.setAttribute('data-raw-reasoning', String(text || ''));
  reasoningEl.hidden = false;
  reasoningEl.open = true;
  const body = reasoningEl.querySelector('.agent-reasoning-body');
  if (body) renderAgentMarkdown(body, text);
  scrollAgentToBottom();
}

function extractAgentPlan(text) {
  const source = String(text || '');
  const headerMatch = source.match(/План (?:проверки|действий):/);
  const headerIndex = headerMatch ? headerMatch.index : -1;
  if (headerIndex < 0) return '';

  const lines = source.slice(headerIndex).split(/\r?\n/);
  const planLines = [];
  let sawStep = false;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!planLines.length) {
      const headerOffset = line.search(/План (?:проверки|действий):/);
      if (headerOffset >= 0) planLines.push(line.slice(headerOffset).trim());
      continue;
    }

    if (/^(\d+[\.)]|[-*])\s+/.test(trimmed)) {
      sawStep = true;
      planLines.push(trimmed.replace(/(\.)(\s*)(Проверю|Уточню|Теперь|Дальше|Дополнительно).*$/i, '$1'));
      continue;
    }

    if (sawStep) break;
    if (trimmed) planLines.push(trimmed);
  }

  return sawStep ? planLines.join('\n').trim() : '';
}

function finishActiveAssistant() {
  const textEl = agentState.activeAssistantTextEl;
  if (textEl && !textEl.textContent.trim()) {
    textEl.textContent = 'Ответ не получен.';
  }
  const message = textEl ? textEl.closest('.agent-message-assistant') : null;
  if (message) {
    message.classList.remove('is-streaming');
    const reasoningEl = message.querySelector('.agent-reasoning');
    if (reasoningEl && !reasoningEl.hidden) reasoningEl.open = false;
    ensureAgentMessageCopyButton(message);
  }
  agentState.activeAssistantMessageEl = null;
  agentState.activeAssistantTextEl = null;
  agentState.activeAssistantReasoningEl = null;
  agentState.activeAssistantPlanEl = null;
  agentState.activeAssistantStepsEl = null;
  agentState.pendingToolIds = [];
  agentState.toolSeq = 0;
  agentState.activeAnswerRaw = '';
  agentState.activeAnswer = '';
  agentState.activeReasoning = '';
  scheduleAgentTranscriptPersist();
}

function parseSseEvent(raw) {
  let eventName = 'message';
  const dataLines = [];
  for (const line of raw.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  let payload = {};
  if (dataLines.length) {
    try {
      payload = JSON.parse(dataLines.join('\n'));
    } catch {
      payload = { message: dataLines.join('\n') };
    }
  }
  return { eventName, payload };
}

async function subscribeAgentTurnStream(chatId, turnId, afterSeq = 0) {
  if (!chatId || !turnId) return;
  if (agentState.streamController) {
    agentState.streamRunId += 1;
    agentState.streamController.abort();
  }
  const streamRunId = ++agentState.streamRunId;
  const controller = new AbortController();
  agentState.streamController = controller;

  try {
    const res = await fetch(
      withToken(`${API_PREFIX}/agent/chats/${encodeURIComponent(chatId)}/turns/${encodeURIComponent(turnId)}/stream`, {
        after_seq: String(afterSeq || 0),
      }),
      {
        headers: { 'Accept': 'text/event-stream' },
        signal: controller.signal,
      },
    );
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.message || data.error || `${res.status}: ${res.statusText}`);
    }
    if (!res.body) throw new Error('Streaming response is not available');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (agentState.streamRunId !== streamRunId) return;
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const raw = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (raw.trim()) {
          const parsed = parseSseEvent(raw);
          handleAgentEvent(parsed.eventName, parsed.payload);
        }
        boundary = buffer.indexOf('\n\n');
      }
    }

    buffer += decoder.decode().replace(/\r\n/g, '\n');
    if (agentState.streamRunId === streamRunId && buffer.trim()) {
      const parsed = parseSseEvent(buffer.trim());
      handleAgentEvent(parsed.eventName, parsed.payload);
    }
  } catch (err) {
    if (agentState.streamRunId !== streamRunId || err.name === 'AbortError') return;
    appendAgentNotice('error', err.message || 'Ошибка подписки на выполнение агента');
    finishActiveAssistant();
  } finally {
    if (agentState.streamRunId === streamRunId) {
      agentState.streamController = null;
      syncAgentBusy();
      updateAgentControls();
      refreshAgentChatsSilently();
    }
  }
}

function handleAgentEvent(eventName, payload) {
  const eventChatId = String(payload?.chat_id || '');
  const eventTurnId = String(payload?.turn_id || '');
  const eventSeq = Number(payload?.seq || 0);
  if (eventTurnId && eventSeq > 0) {
    agentState.lastEventSeqByTurn[eventTurnId] = Math.max(agentState.lastEventSeqByTurn[eventTurnId] || 0, eventSeq);
  }

  if (eventName === 'turn_status') {
    const status = String(payload.status || '');
    if (eventChatId && eventTurnId && status !== 'running') {
      clearAgentRunning(eventChatId, eventTurnId);
      invalidateAgentChatView(eventChatId);
    }
    if (eventChatId && eventChatId !== agentState.chatId) {
      renderAgentChatList();
      refreshAgentChatsSilently();
      return;
    }
    if (eventTurnId && agentState.activeTurnId && eventTurnId !== agentState.activeTurnId) return;
    if (status === 'stopped' && payload.message) {
      appendAgentNotice('warning', payload.message);
    }
    if (status === 'error' && payload.message) {
      appendAgentNotice('error', payload.message);
    }
    finishActiveAssistant();
    updateAgentControls();
    refreshAgentChatsSilently();
    return;
  }

  if ((eventName === 'done' || eventName === 'error') && eventChatId && eventChatId !== agentState.chatId) {
    clearAgentRunning(eventChatId, eventTurnId);
    invalidateAgentChatView(eventChatId);
    renderAgentChatList();
    refreshAgentChatsSilently();
    return;
  }

  if (eventChatId && eventChatId !== agentState.chatId) return;
  if (eventTurnId && agentState.activeTurnId && eventTurnId !== agentState.activeTurnId) return;

  if (eventName === 'start') {
    if (payload.chat_id) {
      activateAgentChat(String(payload.chat_id));
    }
    if (payload.turn_id) {
      setAgentRunning(String(payload.chat_id || agentState.chatId), String(payload.turn_id));
    }
    if (payload.session_id) {
      agentState.sessionId = String(payload.session_id);
      writeAgentSessionId(agentState.sessionId);
    }
    return;
  }
  if (eventName === 'delta') {
    agentState.activeAnswerRaw += String(payload.text || '');
    setActiveAssistantText(stripStreamingToolMarkup(agentState.activeAnswerRaw));
    return;
  }
  if (eventName === 'reasoning_delta') {
    setActiveReasoningText(agentState.activeReasoning + String(payload.text || ''));
    return;
  }
  if (eventName === 'tool_call') {
    appendAgentToolRow('call', payload);
    return;
  }
  if (eventName === 'tool_result') {
    appendAgentToolRow('result', payload);
    return;
  }
  if (eventName === 'plan') {
    renderAgentPlan(payload);
    return;
  }
  if (eventName === 'plan_step') {
    updateAgentPlanStep(payload);
    return;
  }
  if (eventName === 'plan_done') {
    completeAgentPlan(payload);
    return;
  }
  if (eventName === 'usage') {
    renderAgentUsage(payload);
    return;
  }
  if (eventName === 'warning') {
    appendAgentNotice('warning', payload.message || 'Предупреждение');
    return;
  }
  if (eventName === 'error') {
    appendAgentNotice('error', payload.message || payload.error || 'Ошибка агента');
    clearAgentRunning(eventChatId || agentState.chatId, eventTurnId || agentState.activeTurnId);
    invalidateAgentChatView(eventChatId || agentState.chatId);
    finishActiveAssistant();
    updateAgentControls();
    return;
  }
  if (eventName === 'done') {
    const finalAnswer = String(payload.answer || '').trim();
    if (finalAnswer) {
      agentState.activeAnswerRaw = finalAnswer;
      setActiveAssistantText(finalAnswer);
    }
    clearAgentRunning(eventChatId || agentState.chatId, eventTurnId || agentState.activeTurnId);
    invalidateAgentChatView(eventChatId || agentState.chatId);
    finishActiveAssistant();
    updateAgentControls();
  }
}

async function sendAgentMessage(message) {
  const body = $('agent-body');
  if (!body || !AGENT_ENABLED) return;
  if (!agentState.chatId) {
    if (agentState.loadingChat) return;
    await startNewAgentChat();
    if (!agentState.chatId) return;
  }
  if (activeAgentRunningTurn()) {
    appendAgentNotice('warning', 'В этом чате агент уже выполняет задачу.');
    return;
  }

  agentState.autoScroll = true;
  appendAgentMessage('user', message);
  agentState.messages.push({ role: 'user', text: message });

  const assistant = appendAgentMessage('assistant', '');
  agentState.activeAssistantMessageEl = assistant || null;
  agentState.activeAssistantTextEl = assistant?.querySelector('.agent-message-text') || null;
  agentState.activeAssistantReasoningEl = assistant?.querySelector('.agent-reasoning') || null;
  agentState.activeAssistantPlanEl = assistant?.querySelector('.agent-plan') || null;
  agentState.activeAssistantStepsEl = assistant?.querySelector('.agent-tool-steps') || null;
  agentState.pendingToolIds = [];
  agentState.toolSeq = 0;
  agentState.activeAnswer = '';
  agentState.activeReasoning = '';

  const chatId = agentState.chatId;
  updateAgentControls();

  try {
    const payload = await startAgentTurn(
      chatId,
      message,
      readAgentSendContextEnabled() ? agentContextFromState() : {},
      normalizeAgentLlmProfileId(agentState.llmProfileId),
    );
    const turn = payload.turn || {};
    if (!turn.id) throw new Error('Backend не вернул turn id');
    setAgentRunning(chatId, turn.id);
    agentState.lastEventSeqByTurn[turn.id] = Number(turn.last_event_seq || turn.event_count || 0);
    renderAgentChatList();
    updateAgentControls();
    await subscribeAgentTurnStream(chatId, turn.id, agentState.lastEventSeqByTurn[turn.id] || 0);
  } catch (err) {
    if (err.name === 'AbortError') return;
    appendAgentNotice('error', err.message || 'Ошибка агента');
    clearAgentRunning(chatId);
    finishActiveAssistant();
  } finally {
    syncAgentBusy();
    updateAgentControls();
    const input = $('agent-input');
    if (input && AGENT_ENABLED) input.focus();
    refreshAgentChatsSilently();
  }
}

function setDetailCollapsed(collapsed) {
  const workspace = document.querySelector('.analysis-workspace');
  const toggle = $('detail-toggle');
  if (!workspace || !toggle) return;

  workspace.classList.toggle('detail-collapsed', collapsed);
  toggle.textContent = collapsed ? '›' : '‹';
  toggle.title = collapsed ? 'Развернуть детали' : 'Свернуть детали';
  toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  localStorage.setItem('analysisDetailCollapsed', collapsed ? '1' : '0');

  const agentPanel = document.querySelector('.agent-panel');
  if (agentPanel && !workspace.classList.contains('agent-collapsed')) {
    const agentWidth = clampAgentWidth(workspace, Math.round(agentPanel.getBoundingClientRect().width || 0));
    workspace.style.setProperty('--agent-expanded-width', `${agentWidth}px`);
  }
}

function setAgentCollapsed(collapsed) {
  const workspace = document.querySelector('.analysis-workspace');
  const toggle = $('agent-toggle');
  if (!workspace || !toggle) return;

  workspace.classList.toggle('agent-collapsed', collapsed);
  toggle.textContent = collapsed ? '‹' : '›';
  toggle.title = collapsed ? 'Развернуть агента' : 'Свернуть агента';
  toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  localStorage.setItem('analysisAgentCollapsed', collapsed ? '1' : '0');

  if (!collapsed) {
    const agentPanel = document.querySelector('.agent-panel');
    const storedWidth = Number(localStorage.getItem('analysisAgentWidth') || 0);
    const currentWidth = storedWidth || Math.round(agentPanel?.getBoundingClientRect().width || 0);
    const width = clampAgentWidth(workspace, currentWidth);
    workspace.style.setProperty('--agent-expanded-width', `${width}px`);
    localStorage.setItem('analysisAgentWidth', String(Math.round(width)));
  }
}

async function loadAnalysisTree() {
  const treeEl = $('analysis-tree');
  const detailEl = $('analysis-detail');
  if (treeEl) treeEl.innerHTML = '<div class="loading">Загрузка...</div>';
  if (detailEl) detailEl.innerHTML = '<div class="empty-state">Загрузка...</div>';

  try {
    hideAnalysisError();
    const data = await fetchJson(withToken(`${API_PREFIX}/analysis/tree`));
    analysisState.tree = data;
    renderTree(data);
    updateSearchSettingsButton();
    const restored = await restoreAnalysisStateAfterTreeLoad();
    if (!restored) renderProjectDetail();
  } catch (err) {
    showAnalysisError(err.message);
    if (treeEl) treeEl.innerHTML = '<div class="empty-state">Нет данных</div>';
    if (detailEl) detailEl.innerHTML = '<div class="empty-state">Нет данных</div>';
  }
}

function sortCategories(categories) {
  return [...(categories || [])].sort((a, b) => {
    return compareByOrder(a.name, b.name, CATEGORY_ORDER);
  });
}

function sortCommonCategories(categories) {
  return [...(categories || [])].sort((a, b) => {
    return compareByOrder(a.name, b.name, COMMON_CATEGORY_ORDER);
  });
}

function compareByOrder(aName, bName, order) {
  const orderMap = new Map(order.map((name, index) => [categoryOrderKey(name), index]));
  const ai = orderMap.has(categoryOrderKey(aName)) ? orderMap.get(categoryOrderKey(aName)) : -1;
  const bi = orderMap.has(categoryOrderKey(bName)) ? orderMap.get(categoryOrderKey(bName)) : -1;
  if (ai !== -1 || bi !== -1) {
    return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
  }
  return String(aName).localeCompare(String(bName), 'ru');
}

function categoryOrderKey(name) {
  const key = rawCategoryKey(name);
  return CATEGORY_ORDER_ALIASES[key] || key;
}

function rawCategoryKey(name) {
  return String(name || '').replace(/[\s\-_]+/g, '').toLocaleLowerCase('ru');
}

function categoryDisplayName(name) {
  const key = rawCategoryKey(name);
  return NESTED_CATEGORY_LABELS[key] || CATEGORY_DISPLAY_LABELS[key] || name;
}

function splitTreeCategories(categories) {
  const common = [];
  const regular = [];
  const nestedByParent = new Map();
  for (const category of categories || []) {
    const parentKey = NESTED_CATEGORY_PARENT[rawCategoryKey(category.name)];
    if (!parentKey) continue;
    if (!nestedByParent.has(parentKey)) nestedByParent.set(parentKey, []);
    nestedByParent.get(parentKey).push(category);
  }
  for (const category of categories || []) {
    if (NESTED_CATEGORY_PARENT[rawCategoryKey(category.name)]) continue;
    const item = {
      ...category,
      child_categories: sortCategories(nestedByParent.get(rawCategoryKey(category.name)) || []),
    };
    if (COMMON_CATEGORY_SET.has(categoryOrderKey(category.name))) common.push(item);
    else regular.push(item);
  }
  return {
    common: sortCommonCategories(common),
    regular: sortCategories(regular),
  };
}

function categoryBranchHtml(config, cat) {
  const childCategories = cat.child_categories || [];
  const nestedCategoriesHtml = childCategories.length
    ? `<div class="nested-categories">${childCategories.map((child) => categoryBranchHtml(config, child)).join('')}</div>`
    : '';
  const displayName = categoryDisplayName(cat.name);
  return `
    <div class="tree-branch tree-category" data-config="${escapeHtml(config.name)}" data-category="${escapeHtml(cat.name)}">
      <div class="tree-row">
        <button class="tree-toggle" type="button" data-toggle="category"
                aria-label="Развернуть категорию"
                title="Развернуть">+</button>
        <button class="tree-node" type="button" data-kind="category"
                data-config="${escapeHtml(config.name)}"
                data-category="${escapeHtml(cat.name)}"
                title="${escapeHtml(displayName)}">
          ${treeIconHtml(categoryIconName(cat.name))}
          <span class="tree-label">${escapeHtml(displayName)}</span>
          <span class="tree-count">${Number(cat.object_count || 0).toLocaleString()}</span>
        </button>
      </div>
      <div class="tree-children category-objects" hidden>${nestedCategoriesHtml}</div>
    </div>
  `;
}

function renderTree(data) {
  const project = data.project || {};
  const configs = data.configurations || [];
  const treeEl = $('analysis-tree');
  if (!treeEl) return;

  let baseOpened = false;
  const configHtml = configs.map((config) => {
    const categoryGroups = splitTreeCategories(config.categories);
    const commonTotal = categoryGroups.common.reduce((sum, cat) => sum + Number(cat.object_count || 0), 0);
    const isBaseConfig = !config.is_extension && !baseOpened;
    if (isBaseConfig) baseOpened = true;
    const commonHtml = categoryGroups.common.length ? `
      <div class="tree-branch tree-category-group" data-config="${escapeHtml(config.name)}" data-group="common">
        <div class="tree-row">
          <button class="tree-toggle" type="button" data-toggle="group"
                  aria-label="Развернуть группу"
                  title="Развернуть">+</button>
          <button class="tree-node" type="button" data-kind="category-group"
                  data-config="${escapeHtml(config.name)}"
                  data-group="common"
                  title="Общие">
            ${treeIconHtml('common')}
            <span class="tree-label">Общие</span>
            <span class="tree-count">${commonTotal.toLocaleString()}</span>
          </button>
        </div>
        <div class="tree-children" hidden>
          ${categoryGroups.common.map((cat) => categoryBranchHtml(config, cat)).join('')}
        </div>
      </div>
    ` : '';
    const categoriesHtml = [
      commonHtml,
      ...categoryGroups.regular.map((cat) => categoryBranchHtml(config, cat)),
    ].join('');

    return `
      <div class="tree-branch tree-config${isBaseConfig ? ' is-open' : ''}" data-config="${escapeHtml(config.name)}">
        <div class="tree-row">
          <button class="tree-toggle" type="button" data-toggle="config"
                  aria-label="${isBaseConfig ? 'Свернуть конфигурацию' : 'Развернуть конфигурацию'}"
                  title="${isBaseConfig ? 'Свернуть' : 'Развернуть'}">${isBaseConfig ? '-' : '+'}</button>
          <button class="tree-node" type="button" data-kind="config"
                  data-config="${escapeHtml(config.name)}"
                  title="${escapeHtml(displayConfigName(config.name))}">
            ${treeIconHtml('configuration')}
            <span class="tree-label">${escapeHtml(displayConfigName(config.name))}</span>
            ${config.is_extension ? '<span class="tree-pill">ext</span>' : ''}
          </button>
        </div>
        <div class="tree-children" ${isBaseConfig ? '' : 'hidden'}>${categoriesHtml}</div>
      </div>
    `;
  }).join('');

  treeEl.innerHTML = `
    <div class="tree-branch tree-project is-open">
      <div class="tree-row">
        <button class="tree-toggle" type="button" data-toggle="project"
                aria-label="Свернуть проект" title="Свернуть">-</button>
        <button class="tree-node root-node" type="button" data-kind="project"
                title="${escapeHtml(project.name || 'Project')}">
          ${treeIconHtml('project')}
          <span class="tree-label">${escapeHtml(project.name || 'Project')}</span>
        </button>
      </div>
      <div class="tree-children">${configHtml}</div>
    </div>
  `;
  markActiveTreeNode();
}

function searchCheckboxHtml(scope, value, label, checked) {
  return `
    <label class="search-settings-option">
      <input type="checkbox"
             data-search-scope="${escapeHtml(scope)}"
             value="${escapeHtml(value)}"
             ${checked ? 'checked' : ''}>
      <span>${escapeHtml(label)}</span>
    </label>
  `;
}

function searchTypeColumnsHtml() {
  const labels = new Map(SEARCH_TYPE_OPTIONS);
  const selected = analysisState.searchTypes || [];
  return `
    <div class="search-settings-columns">
      ${SEARCH_TYPE_COLUMNS.map((column) => `
        <div class="search-settings-column">
          ${column.map((value) => {
            const label = labels.get(value);
            if (!label) return '';
            return searchCheckboxHtml('type', value, label, selected.includes(value));
          }).join('')}
        </div>
      `).join('')}
    </div>
  `;
}

function searchDefaultValues(options) {
  return options.map(([value]) => value);
}

function equalSearchSet(values, options) {
  const expected = new Set(searchDefaultValues(options));
  const actual = new Set(values || []);
  if (expected.size !== actual.size) return false;
  return [...expected].every((value) => actual.has(value));
}

function hasCustomSearchSettings() {
  return Boolean(analysisState.searchConfig)
    || !equalSearchSet(analysisState.searchTypes, SEARCH_TYPE_OPTIONS)
    || !equalSearchSet(analysisState.searchFields, SEARCH_FIELD_OPTIONS);
}

function updateSearchSettingsButton() {
  const button = $('analysis-search-settings');
  if (!button) return;
  const isCustom = hasCustomSearchSettings();
  button.classList.toggle('is-custom', isCustom);
  button.title = isCustom ? 'Параметры поиска изменены' : 'Параметры поиска';
}

function showSearchSettingsModal() {
  const modal = ensureSearchSettingsModal();
  renderSearchSettingsModal(modal);
  modal.hidden = false;
}

function closeSearchSettingsModal() {
  const modal = $('analysis-search-settings-modal');
  if (modal) modal.hidden = true;
}

function ensureSearchSettingsModal() {
  let modal = $('analysis-search-settings-modal');
  if (modal) return modal;

  modal = document.createElement('div');
  modal.id = 'analysis-search-settings-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="search-settings-close"></div>
    <section class="condition-modal-dialog search-settings-dialog" role="dialog" aria-modal="true"
             aria-labelledby="analysis-search-settings-title">
      <header class="condition-modal-header">
        <div>
          <h3 id="analysis-search-settings-title">Параметры поиска</h3>
          <p>Ограничение области поиска в структуре конфигураций.</p>
        </div>
        <button class="panel-icon-button" type="button" data-role="search-settings-close"
                title="Закрыть">×</button>
      </header>
      <div class="search-settings-body" data-role="search-settings-body"></div>
      <footer class="condition-modal-footer search-settings-footer">
        <button class="summary-action-button summary-secondary-button" type="button"
                data-role="search-settings-reset">Сбросить</button>
        <button class="summary-action-button summary-secondary-button" type="button"
                data-role="search-settings-close">Отмена</button>
        <button class="summary-action-button" type="button"
                data-role="search-settings-apply">Применить</button>
      </footer>
    </section>
  `;
  document.body.appendChild(modal);
  modal.addEventListener('click', (event) => {
    if (event.target.closest('[data-role="search-settings-close"]')) {
      closeSearchSettingsModal();
      return;
    }
    if (event.target.closest('[data-role="search-settings-reset"]')) {
      resetSearchSettingsModal(modal);
      return;
    }
    if (event.target.closest('[data-role="search-settings-apply"]')) {
      applySearchSettingsModal(modal);
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) closeSearchSettingsModal();
  });
  return modal;
}

function renderSearchSettingsModal(modal) {
  const body = modal.querySelector('[data-role="search-settings-body"]');
  if (!body) return;
  const configs = analysisState.tree?.configurations || [];
  body.innerHTML = `
    <section class="search-settings-section">
      <label class="search-settings-label" for="search-settings-config">Конфигурация</label>
      <select id="search-settings-config" class="search-settings-select" data-role="search-settings-config">
        <option value="">Все конфигурации</option>
        ${configs.map((config) => `
          <option value="${escapeHtml(config.name || '')}">
            ${escapeHtml(displayConfigName(config.name || ''))}
          </option>
        `).join('')}
      </select>
    </section>
    <section class="search-settings-section">
      <div class="search-settings-title">Типы узлов</div>
      ${searchTypeColumnsHtml()}
    </section>
    <section class="search-settings-section">
      <div class="search-settings-title">Где искать</div>
      <div class="search-settings-grid">
        ${SEARCH_FIELD_OPTIONS.map(([value, label]) => {
          return searchCheckboxHtml('field', value, label, (analysisState.searchFields || []).includes(value));
        }).join('')}
      </div>
    </section>
  `;
  const select = body.querySelector('[data-role="search-settings-config"]');
  if (select) {
    const current = analysisState.searchConfig || '';
    select.value = configs.some((config) => config.name === current) ? current : '';
  }
}

function resetSearchSettingsModal(modal) {
  const body = modal.querySelector('[data-role="search-settings-body"]');
  if (!body) return;
  const select = body.querySelector('[data-role="search-settings-config"]');
  if (select) select.value = '';
  for (const input of body.querySelectorAll('input[data-search-scope]')) {
    input.checked = true;
  }
}

function selectedSearchValues(modal, scope, options) {
  const values = [...modal.querySelectorAll(`input[data-search-scope="${scope}"]:checked`)]
    .map((input) => input.value)
    .filter(Boolean);
  return values.length ? values : searchDefaultValues(options);
}

function applySearchSettingsModal(modal) {
  const select = modal.querySelector('[data-role="search-settings-config"]');
  analysisState.searchConfig = select?.value || '';
  analysisState.searchTypes = selectedSearchValues(modal, 'type', SEARCH_TYPE_OPTIONS);
  analysisState.searchFields = selectedSearchValues(modal, 'field', SEARCH_FIELD_OPTIONS);
  updateSearchSettingsButton();
  closeSearchSettingsModal();
  restartAnalysisSearch();
}

function restartAnalysisSearch() {
  const filter = $('analysis-filter');
  const query = String(filter?.value || '').trim();
  analysisState.searchData = null;
  analysisState.searchVisibleCount = 0;
  analysisState.searchPrefetching = false;
  analysisState.searchLoadMorePending = false;
  if (query) scheduleAnalysisSearch(query);
}

function searchRequestParams(query, limit, offset) {
  return {
    q: query,
    limit,
    offset,
    config: analysisState.searchConfig || '',
    types: (analysisState.searchTypes || []).join(','),
    fields: (analysisState.searchFields || []).join(','),
  };
}

function scheduleAnalysisSearch(value) {
  const query = String(value || '').trim();
  analysisState.searchQuery = query;
  if (analysisState.searchTimer) {
    clearTimeout(analysisState.searchTimer);
    analysisState.searchTimer = null;
  }
  if (!query) {
    analysisState.searchData = null;
    analysisState.searchVisibleCount = 0;
    analysisState.searchPrefetching = false;
    analysisState.searchLoadMorePending = false;
    if (analysisState.tree) renderTree(analysisState.tree);
    return;
  }
  analysisState.searchTimer = setTimeout(() => {
    analysisState.searchTimer = null;
    runAnalysisSearch(query);
  }, SEARCH_DEBOUNCE_MS);
}

function runAnalysisSearchNow(value) {
  const query = String(value || '').trim();
  analysisState.searchQuery = query;
  if (analysisState.searchTimer) {
    clearTimeout(analysisState.searchTimer);
    analysisState.searchTimer = null;
  }
  if (!query) {
    analysisState.searchData = null;
    analysisState.searchVisibleCount = 0;
    analysisState.searchPrefetching = false;
    analysisState.searchLoadMorePending = false;
    if (analysisState.tree) renderTree(analysisState.tree);
    return;
  }
  runAnalysisSearch(query);
}

async function runAnalysisSearch(query) {
  const seq = ++analysisState.searchSeq;
  const treeEl = $('analysis-tree');
  if (treeEl) treeEl.innerHTML = '<div class="search-state">Поиск...</div>';
  try {
    hideAnalysisError();
    const data = await fetchJson(withToken(
      `${API_PREFIX}/analysis/search`,
      searchRequestParams(query, 100, 0),
    ));
    if (seq !== analysisState.searchSeq || analysisState.searchQuery !== query) return;
    analysisState.searchData = data;
    const requestedVisible = Math.max(50, Number(analysisState.pendingSearchVisibleCount || 0));
    analysisState.pendingSearchVisibleCount = 0;
    analysisState.searchVisibleCount = Math.min(requestedVisible, (data.items || []).length);
    analysisState.searchPrefetching = false;
    analysisState.searchLoadMorePending = false;
    renderSearchResults(data);
  } catch (err) {
    if (seq !== analysisState.searchSeq) return;
    showAnalysisError(err.message);
    if (treeEl) treeEl.innerHTML = '<div class="search-state">Поиск не выполнен</div>';
  }
}

function renderSearchResults(data) {
  const treeEl = $('analysis-tree');
  if (!treeEl) return;
  const loadedItems = data.items || [];
  const visibleCount = Math.min(
    Number(analysisState.searchVisibleCount || 0),
    loadedItems.length,
  );
  const items = loadedItems.slice(0, visibleCount);
  if (!loadedItems.length) {
    treeEl.innerHTML = `
      <div class="search-results">
        <div class="search-summary">Ничего не найдено</div>
      </div>
    `;
    return;
  }

  const groups = [];
  const byTitle = new Map();
  items.forEach((item) => {
    const title = searchResultGroupTitle(item);
    if (!byTitle.has(title)) {
      const group = { title, items: [] };
      byTitle.set(title, group);
      groups.push(group);
    }
    byTitle.get(title).items.push(item);
  });

  const shown = visibleCount;
  const total = Number(data.total || 0);
  treeEl.innerHTML = `
    <div class="search-results">
      <div class="search-summary">Найдено ${total.toLocaleString()} · показано ${shown.toLocaleString()}</div>
      ${groups.map((group) => `
        <div class="search-group">
          <div class="search-group-title">
            <span>${escapeHtml(group.title)}</span>
            <span>${group.items.length.toLocaleString()}</span>
          </div>
          ${group.items.map((item) => searchResultHtml(item)).join('')}
        </div>
      `).join('')}
      ${total > shown ? `
        <button class="load-more-button search-load-more" type="button" data-kind="search-load-more">
          Загрузить еще · показано ${shown.toLocaleString()} из ${total.toLocaleString()}
        </button>
      ` : ''}
    </div>
  `;
  markActiveTreeNode();
}

function searchResultHtml(item) {
  const kind = item.kind === 'object' ? 'object' : 'node';
  const path = formatSearchPath(item);
  const subtitle = [
    item.synonym,
    item.config_name ? displayConfigName(item.config_name) : '',
  ].filter(Boolean).join(' · ');
  const icon = searchResultIcon(item);
  const iconState = extensionObjectState(item);
  const adoption = baseAdoptionMarkerHtml(item, item.config_name);
  return `
    <button class="tree-node search-result-node" type="button"
            data-kind="${escapeHtml(kind)}"
            data-ref="${escapeHtml(item.ref || '')}"
            title="${escapeHtml(path || item.label || '')}">
      ${treeIconWithStateHtml(icon, iconState)}
      <span class="search-result-main">
        <span class="search-result-label">${escapeHtml(item.label || '')}</span>
        ${subtitle ? `<span class="search-result-subtitle">${escapeHtml(subtitle)}</span>` : ''}
        <span class="search-result-path">${escapeHtml(path)}</span>
      </span>
      ${adoption}
    </button>
  `;
}

function loadMoreSearchResults() {
  const data = analysisState.searchData;
  const query = analysisState.searchQuery;
  if (!data || !query) return;
  const total = Number(data.total || 0);
  const items = data.items || [];
  const visible = Number(analysisState.searchVisibleCount || 0);
  if (visible >= total) return;

  if (items.length > visible) {
    analysisState.searchVisibleCount = Math.min(visible + 50, items.length, total);
    renderSearchResults(data);
    prefetchSearchResults(query);
    return;
  }

  document.querySelectorAll('.search-load-more').forEach((button) => {
    button.disabled = true;
    button.textContent = 'Загрузка...';
  });
  if (analysisState.searchPrefetching) {
    analysisState.searchLoadMorePending = true;
    return;
  }
  fetchSearchResultsPage(query, items.length);
}

async function prefetchSearchResults(query) {
  const data = analysisState.searchData;
  if (!data || analysisState.searchPrefetching) return;
  const total = Number(data.total || 0);
  const offset = (data.items || []).length;
  if (offset >= total) return;
  analysisState.searchPrefetching = true;
  try {
    await fetchSearchResultsPage(query, offset, { background: true });
  } finally {
    if (analysisState.searchQuery === query) {
      analysisState.searchPrefetching = false;
      const current = analysisState.searchData;
      const loaded = (current?.items || []).length;
      const visible = Number(analysisState.searchVisibleCount || 0);
      const total = Number(current?.total || 0);
      if (visible >= loaded && loaded < total) prefetchSearchResults(query);
    }
  }
}

async function fetchSearchResultsPage(query, offset, options = {}) {
  const seq = analysisState.searchSeq;
  const data = analysisState.searchData;
  try {
    hideAnalysisError();
    const next = await fetchJson(withToken(
      `${API_PREFIX}/analysis/search`,
      searchRequestParams(query, 50, offset),
    ));
    if (seq !== analysisState.searchSeq || analysisState.searchQuery !== query || !analysisState.searchData) return;
    const known = new Set((analysisState.searchData.items || []).map((item) => item.ref));
    const incoming = (next.items || []).filter((item) => !known.has(item.ref));
    analysisState.searchData.items = [...(analysisState.searchData.items || []), ...incoming];
    analysisState.searchData.total = next.total;
    analysisState.searchData.limit = next.limit;
    analysisState.searchData.offset = 0;
    if ((options.background && analysisState.searchLoadMorePending) || (!options.background && data)) {
      analysisState.searchLoadMorePending = false;
      analysisState.searchVisibleCount = Math.min(
        Number(analysisState.searchVisibleCount || 0) + 50,
        (analysisState.searchData.items || []).length,
        Number(analysisState.searchData.total || 0),
      );
    }
    renderSearchResults(analysisState.searchData);
  } catch (err) {
    if (seq !== analysisState.searchSeq) return;
    showAnalysisError(err.message);
    renderSearchResults(analysisState.searchData || { items: [], total: 0 });
  }
}

async function toggleTreeBranch(toggle) {
  const branch = toggle.closest('.tree-branch');
  if (!branch) return;
  const children = branch.querySelector(':scope > .tree-children');
  const shouldOpen = children?.hidden;
  if (toggle.dataset.toggle === 'category' && shouldOpen) {
    const node = branch.querySelector('.tree-node[data-kind="category"]');
    if (node) await selectCategory(node, { expand: true, renderDetail: false });
  }
  if (toggle.dataset.toggle === 'object' && shouldOpen) {
    const ref = branch.dataset.ref;
    const container = branch.querySelector(':scope > .tree-children');
    if (ref && container && branch.dataset.hasComposition === '1') await renderObjectTreeChildren(ref, container);
  }
  if (toggle.dataset.toggle === 'form' && shouldOpen) {
    await renderFormTreeChildren(branch);
  }
  setTreeBranchOpen(branch, Boolean(shouldOpen));
}

function setTreeBranchOpen(branch, open) {
  const children = branch.querySelector(':scope > .tree-children');
  const toggle = branch.querySelector(':scope > .tree-row > .tree-toggle');
  if (!children || !toggle) return;
  children.hidden = !open;
  branch.classList.toggle('is-open', open);
  toggle.textContent = open ? '-' : '+';
  toggle.title = open ? 'Свернуть' : 'Развернуть';
  toggle.setAttribute('aria-label', open ? 'Свернуть' : 'Развернуть');
}

function findCategoryBranch(config, category) {
  return [...document.querySelectorAll('.tree-category')].find((branch) => {
    return branch.dataset.config === config && branch.dataset.category === category;
  }) || null;
}

function findConfigBranch(config) {
  return [...document.querySelectorAll('.tree-config')].find((branch) => {
    return branch.dataset.config === config;
  }) || null;
}

function categoryCacheKey(config, category) {
  return `${config}/${category}`;
}

function findObjectBranch(ref) {
  return [...document.querySelectorAll('.tree-object-branch')].find((branch) => {
    return branch.dataset.ref === ref;
  }) || null;
}

async function getObjectData(ref) {
  if (!analysisState.objectCache.has(ref)) {
    const data = await fetchJson(withToken(`${API_PREFIX}/analysis/object`, { ref }));
    analysisState.objectCache.set(ref, data);
  }
  return analysisState.objectCache.get(ref);
}

async function getFormTreeData(ref) {
  if (!analysisState.formTreeCache.has(ref)) {
    const data = await fetchJson(withToken(`${API_PREFIX}/analysis/form-tree`, { ref }));
    analysisState.formTreeCache.set(ref, data);
  }
  return analysisState.formTreeCache.get(ref);
}

async function getModuleData({ moduleId = '', ownerRef = '', moduleType = '' }) {
  ({ moduleId, ownerRef, moduleType } = normalizeModuleRequest({ moduleId, ownerRef, moduleType }));
  const key = moduleSelectionKey(moduleId, ownerRef, moduleType);
  if (!key) throw new Error('module_ref_required');
  if (!analysisState.moduleCache.has(key)) {
    const data = await fetchJson(withToken(`${API_PREFIX}/analysis/module`, {
      id: moduleId,
      owner_ref: ownerRef,
      module_type: moduleType,
    }));
    analysisState.moduleCache.set(key, data);
  }
  return analysisState.moduleCache.get(key);
}

function currentModuleRequest() {
  if (analysisState.selectedKind === 'module') {
    return {
      moduleId: analysisState.selectedModuleId || '',
      ownerRef: analysisState.selectedModuleOwner || '',
      moduleType: analysisState.selectedModuleType || '',
    };
  }
  if (analysisState.selectedObject && isCommonModuleObject(analysisState.selectedObject)) {
    return {
      moduleId: '',
      ownerRef: analysisState.selectedObject.identity?.qualified_name || analysisState.selectedRef || '',
      moduleType: 'CommonModule',
    };
  }
  return { moduleId: '', ownerRef: '', moduleType: '' };
}

async function getModuleUnitsData({ moduleId = '', ownerRef = '', moduleType = '' }) {
  ({ moduleId, ownerRef, moduleType } = normalizeModuleRequest({ moduleId, ownerRef, moduleType }));
  const key = moduleSelectionKey(moduleId, ownerRef, moduleType);
  if (!key) throw new Error('module_ref_required');
  if (!analysisState.moduleUnitsCache.has(key)) {
    const data = await fetchJson(withToken(`${API_PREFIX}/analysis/module/code-units`, {
      id: moduleId,
      owner_ref: ownerRef,
      module_type: moduleType,
    }));
    analysisState.moduleUnitsCache.set(key, data);
  }
  return analysisState.moduleUnitsCache.get(key);
}

function objectStructureSections(data) {
  const structure = data.structure || {};
  const modules = objectGroupedModules(data);
  const isCommonForm = isCommonFormObjectData(data);
  const order = [
    'standard_attributes',
    'attributes',
    'tabular_parts',
    'resources',
    'dimensions',
    'journal_graphs',
    'forms',
    'commands',
    'layouts',
    'enum_values',
    'predefined',
  ];
  const sections = order
    .map((section) => ({
      key: section,
      title: STRUCTURE_TITLES[section] || section,
      items: structure[section] || [],
    }))
    .filter((section) => {
      if (!section.items.length) return false;
      if (isCommonForm && analysisState.formTreeEnabled && section.key === 'commands') return false;
      return true;
    });

  if (modules.length > 0) {
    sections.push({
      key: 'modules',
      title: STRUCTURE_TITLES.modules,
      items: modules,
    });
  }

  return sections;
}

function moduleIsFormModule(item) {
  return item?.module_type === 'FormModule';
}

function moduleIsCommandModule(item) {
  return item?.module_type === 'CommandModule';
}

function objectRefFromData(data) {
  return data?.identity?.qualified_name || analysisState.selectedObject?.identity?.qualified_name || '';
}

function isCommonFormCategory(category) {
  return String(category || '') === 'ОбщиеФормы';
}

function isCommonFormObjectData(data) {
  return isCommonFormCategory(data?.identity?.category);
}

function isCommonFormTreeItem(item, data) {
  return Boolean(item?.qualified_name) && isCommonFormCategory(data?.category);
}

function objectDirectModule(data) {
  const objectRef = objectRefFromData(data);
  if (!objectRef) return null;
  const category = data?.identity?.category || '';
  const directTypes = new Set();
  if (category === 'ОбщиеФормы') directTypes.add('CommonFormModule');
  if (category === 'ОбщиеКоманды') directTypes.add('CommandModule');
  if (!directTypes.size) return null;
  return (data.modules || []).find((item) => {
    return directTypes.has(item?.module_type) && (item.owner_qn || objectRef) === objectRef;
  }) || null;
}

function objectNonFormModules(data) {
  return (data.modules || []).filter((item) => !moduleIsFormModule(item));
}

function objectGroupedModules(data) {
  const objectRef = objectRefFromData(data);
  const directModule = objectDirectModule(data);
  return objectNonFormModules(data).filter((item) => {
    if (directModule && moduleSelectionKey(item.module_id, item.owner_qn || objectRef, item.module_type)
        === moduleSelectionKey(directModule.module_id, directModule.owner_qn || objectRef, directModule.module_type)) {
      return false;
    }
    if (moduleIsCommandModule(item) && (item.owner_qn || '') && item.owner_qn !== objectRef) return false;
    return true;
  });
}

function formModuleForForm(data, formRef) {
  if (!formRef) return null;
  return (data.modules || []).find((item) => moduleIsFormModule(item) && item.owner_qn === formRef) || null;
}

function commandModuleForCommand(data, commandRef) {
  if (!commandRef) return null;
  return (data.modules || []).find((item) => moduleIsCommandModule(item) && item.owner_qn === commandRef) || null;
}

function hasTreeComposition(item) {
  return [
    'attributes',
    'standard_attributes',
    'tabular_parts',
    'resources',
    'dimensions',
    'forms',
    'commands',
    'layouts',
    'journal_graphs',
    'enum_values',
    'predefined',
    'modules',
  ].some((key) => Number(item?.[key] || 0) > 0);
}

function structureItemIcon(section) {
  const icons = {
    standard_attributes: 'standard_attribute',
    attributes: 'attribute',
    tabular_parts: 'tabular_section',
    tabular_part_attributes: 'attribute',
    resources: 'register_resource',
    dimensions: 'register_dimension',
    forms: 'form',
    commands: 'command',
    layouts: 'template',
    journal_graphs: 'journal_graph',
    enum_values: 'enum_value',
    predefined: 'predefined',
    modules: 'module',
    form_attributes: 'attribute',
    form_controls: 'form_control_field',
  };
  return icons[section] || 'common';
}

function searchResultIcon(item) {
  if (item?.kind === 'object') return categoryIconName(item.category);
  return structureItemIcon(item?.section);
}

function searchResultGroupTitle(item) {
  if (item?.kind === 'object') return item.category || 'Объекты';
  return STRUCTURE_TITLES[item?.section] || 'Элементы';
}

function formatSearchPath(item) {
  return item?.path || item?.ref || '';
}

function treeLeafHtml({
  ref = '',
  label = '',
  title = '',
  icon = 'common',
  state = '',
  adoption = '',
  kind = 'node',
  extraClass = '',
}) {
  const dataAttr = kind === 'node'
    ? `data-kind="node" data-ref="${escapeHtml(ref)}"`
    : `data-kind="${escapeHtml(kind)}"`;
  return `
    <div class="tree-branch${extraClass ? ` ${escapeHtml(extraClass)}` : ''}">
      <div class="tree-row">
        <span class="tree-toggle-spacer"></span>
        <button class="tree-node tree-structure-node" type="button"
                ${dataAttr}
                title="${escapeHtml(title || label)}">
          ${treeIconWithStateHtml(icon, state)}
          <span class="tree-label">${escapeHtml(label)}</span>
          ${adoption}
        </button>
      </div>
    </div>
  `;
}

function structureItemTreeHtml(item, section, childHtml = '', configName = '', options = {}) {
  const ref = item.qualified_name || '';
  const label = item.name || '';
  const title = fullTreeTitle(item.name, item.synonym, item.type);
  const icon = options.icon || structureItemIcon(section);
  const iconState = extensionObjectState(item);
  const adoption = baseAdoptionMarkerHtml(item, configName);
  if (!childHtml) {
    return treeLeafHtml({ ref, label, title, icon, state: iconState, adoption });
  }
  const branchClass = options.branchClass || 'tree-structure-branch';
  const toggleKind = options.toggle || 'plain';
  const extraAttrs = options.extraAttrs || '';
  return `
    <div class="tree-branch ${escapeHtml(branchClass)}" data-ref="${escapeHtml(ref)}" ${extraAttrs}>
      <div class="tree-row">
        <button class="tree-toggle" type="button" data-toggle="${escapeHtml(toggleKind)}"
                aria-label="Развернуть" title="Развернуть">+</button>
        <button class="tree-node tree-structure-node" type="button"
                data-kind="node" data-ref="${escapeHtml(ref)}"
                title="${escapeHtml(title || label)}">
          ${treeIconWithStateHtml(icon, iconState)}
          <span class="tree-label">${escapeHtml(label)}</span>
          ${adoption}
        </button>
      </div>
      <div class="tree-children" hidden>${childHtml}</div>
    </div>
  `;
}

function moduleTreeItemHtml(item, configName = '') {
  const label = item.tree_label || item.module_name || item.module_type || 'Модуль';
  const moduleId = item.module_id || '';
  const ownerRef = item.owner_qn || analysisState.selectedObject?.identity?.qualified_name || '';
  const moduleType = item.module_type || '';
  const key = moduleSelectionKey(moduleId, ownerRef, moduleType);
  return `
    <div class="tree-branch tree-module-branch"
         data-module-key="${escapeHtml(key)}"
         data-module-id="${escapeHtml(moduleId)}"
         data-owner-ref="${escapeHtml(ownerRef)}"
         data-module-type="${escapeHtml(moduleType)}">
      <div class="tree-row">
        <span class="tree-toggle-spacer"></span>
        <button class="tree-node tree-structure-node tree-module-node" type="button"
                data-kind="module"
                data-module-id="${escapeHtml(moduleId)}"
                data-owner-ref="${escapeHtml(ownerRef)}"
                data-module-type="${escapeHtml(moduleType)}"
                title="${escapeHtml(fullTreeTitle(label, moduleType))}">
          ${treeIconWithStateHtml('module', extensionObjectState(item))}
          <span class="tree-label">${escapeHtml(label)}</span>
          ${baseAdoptionMarkerHtml(item, configName)}
        </button>
      </div>
    </div>
  `;
}

function moduleTreeLabel(item) {
  if (item?.module_type === 'CommonFormModule' || item?.module_type === 'FormModule') return 'Модуль формы';
  if (item?.module_type === 'CommandModule') return 'Модуль команды';
  return 'Модуль';
}

function formControlIcon(item) {
  const type = String(item?.type || item?.control_type || '').toLowerCase();
  const name = String(item?.name || '').toLowerCase();
  const value = `${type} ${name}`;
  if (value.includes('флаж') || value.includes('check')) return 'form_control_check';
  if (value.includes('переключ') || value.includes('radio')) return 'form_control_radio';
  if (value.includes('команд') || value.includes('command')) return 'form_control_command_panel';
  if (type === 'страница' || type === 'страницы' || type === 'page' || type === 'pages') return 'form_control_group';
  if (value.includes('групп') || value.includes('group')) return 'form_control_group';
  if (value.includes('кноп') || value.includes('button')) return 'form_control_button';
  if (value.includes('таблиц') || value.includes('table')) return 'form_control_table';
  if (value.includes('колон') || value.includes('column')) return 'form_control_column';
  if (value.includes('картин') || value.includes('picture')) return 'common_picture';
  if (value.includes('декорац') || value.includes('надпис') || value.includes('label') || value.includes('decoration')) {
    return 'form_control_decoration';
  }
  if (value.includes('поиск') || value.includes('search')) return 'form_control_search';
  return 'form_control_field';
}

function sortFormTreeItems(items) {
  return [...(items || [])].sort((a, b) => {
    const ao = Number(a?.order || 0);
    const bo = Number(b?.order || 0);
    if (ao !== bo) return ao - bo;
    return String(a?.name || '').localeCompare(String(b?.name || ''), 'ru');
  });
}

function buildFormControlTree(items) {
  const byRef = new Map();
  for (const item of items || []) {
    const ref = item?.qualified_name || '';
    if (!ref) continue;
    byRef.set(ref, { ...item, children: [] });
  }
  const roots = [];
  for (const item of byRef.values()) {
    const parent = byRef.get(item.parent_qualified_name || '');
    if (parent) {
      parent.children.push(item);
    } else {
      roots.push(item);
    }
  }
  const sortBranch = (nodes) => {
    nodes.sort((a, b) => {
      const ao = Number(a?.order || 0);
      const bo = Number(b?.order || 0);
      if (ao !== bo) return ao - bo;
      return String(a?.name || '').localeCompare(String(b?.name || ''), 'ru');
    });
    nodes.forEach((node) => sortBranch(node.children || []));
  };
  sortBranch(roots);
  return roots;
}

function formControlTreeItemHtml(item, configName = '') {
  const childHtml = (item.children || [])
    .map((child) => formControlTreeItemHtml(child, configName))
    .join('');
  return structureItemTreeHtml(item, 'form_controls', childHtml, configName, {
    icon: formControlIcon(item),
    branchClass: 'tree-structure-branch tree-form-control-branch',
  });
}

function formTreeSectionBranchHtml({ formRef = '', section = '', title = '', icon = 'common', count = 0, childHtml = '' }) {
  if (!count || !childHtml) return '';
  return `
    <div class="tree-branch tree-form-section-branch"
         data-form-ref="${escapeHtml(formRef)}"
         data-section="${escapeHtml(section)}">
      <div class="tree-row">
        <button class="tree-toggle" type="button" data-toggle="plain"
                aria-label="Развернуть" title="Развернуть">+</button>
        <button class="tree-node tree-object-section-node" type="button"
                data-kind="form-section"
                data-form-ref="${escapeHtml(formRef)}"
                data-section="${escapeHtml(section)}"
                title="${escapeHtml(title)}">
          ${treeIconHtml(icon)}
          <span class="tree-label">${escapeHtml(title)}</span>
          <span class="tree-count">${count.toLocaleString()}</span>
        </button>
      </div>
      <div class="tree-children" hidden>${childHtml}</div>
    </div>
  `;
}

function formExtraSectionsHtml(formRef, data, configName = '') {
  const structure = data?.structure || {};
  const attributes = sortFormTreeItems(structure.attributes || []);
  const commands = sortFormTreeItems(structure.commands || []);
  const controls = buildFormControlTree(structure.controls || []);
  return [
    formTreeSectionBranchHtml({
      formRef,
      section: 'form_attributes',
      title: 'Реквизиты',
      icon: 'attribute',
      count: attributes.length,
      childHtml: attributes.map((item) => structureItemTreeHtml(item, 'form_attributes', '', configName)).join(''),
    }),
    formTreeSectionBranchHtml({
      formRef,
      section: 'form_commands',
      title: 'Команды',
      icon: 'command',
      count: commands.length,
      childHtml: commands.map((item) => structureItemTreeHtml(item, 'commands', '', configName)).join(''),
    }),
    formTreeSectionBranchHtml({
      formRef,
      section: 'form_controls',
      title: 'Элементы',
      icon: 'form_elements',
      count: (structure.controls || []).length,
      childHtml: controls.map((item) => formControlTreeItemHtml(item, configName)).join(''),
    }),
  ].filter(Boolean).join('');
}

async function commonFormExtraSectionsHtml(formRef, objectData) {
  if (!analysisState.formTreeEnabled || !isCommonFormObjectData(objectData)) return '';
  try {
    const formData = await getFormTreeData(formRef);
    const configName = objectData?.identity?.config_name || formData?.identity?.config_name || '';
    return formExtraSectionsHtml(formRef, formData, configName);
  } catch (err) {
    showAnalysisError(err.message);
    return '<div class="tree-empty-note">Не удалось загрузить состав формы</div>';
  }
}

function formTreeItemHtml(data, item, configName = '') {
  const module = formModuleForForm(data, item.qualified_name);
  const moduleHtml = module
    ? moduleTreeItemHtml({ ...module, tree_label: moduleTreeLabel(module), owner_qn: module.owner_qn || item.qualified_name }, configName)
    : '';
  if (!analysisState.formTreeEnabled) {
    return structureItemTreeHtml(item, 'forms', moduleHtml, configName);
  }
  const formRef = item.qualified_name || '';
  const extraPlaceholder = `
    <div class="tree-form-extra" data-form-extra-container data-form-ref="${escapeHtml(formRef)}">
      <div class="loading inline">Загрузка состава формы...</div>
    </div>
  `;
  return structureItemTreeHtml(item, 'forms', `${moduleHtml}${extraPlaceholder}`, configName, {
    branchClass: 'tree-structure-branch tree-form-branch',
    toggle: 'form',
    extraAttrs: `data-form-ref="${escapeHtml(formRef)}" data-config-name="${escapeHtml(configName)}" data-form-extra="1"`,
  });
}

async function renderFormTreeChildren(branch) {
  if (!branch || branch.dataset.formExtra !== '1') return;
  const formRef = branch.dataset.formRef || branch.dataset.ref || '';
  const container = branch.querySelector(':scope > .tree-children > [data-form-extra-container]');
  if (!formRef || !container || container.dataset.loaded === '1') return;
  container.innerHTML = '<div class="loading inline">Загрузка состава формы...</div>';
  try {
    const data = await getFormTreeData(formRef);
    const configName = branch.dataset.configName || data?.identity?.config_name || '';
    const html = formExtraSectionsHtml(formRef, data, configName);
    container.dataset.loaded = '1';
    container.innerHTML = html || '<div class="tree-empty-note">Нет реквизитов, команд или элементов</div>';
    markActiveTreeNode();
  } catch (err) {
    container.innerHTML = '<div class="tree-empty-note">Не удалось загрузить состав формы</div>';
    showAnalysisError(err.message);
  }
}

async function refreshRenderedObjectTreeChildren() {
  const containers = [...document.querySelectorAll('.tree-object-branch > .tree-children[data-loaded="1"]')];
  for (const container of containers) {
    const branch = container.closest('.tree-object-branch');
    const ref = branch?.dataset.ref || '';
    if (!branch || !ref) continue;
    const wasOpen = branch.classList.contains('is-open');
    delete container.dataset.loaded;
    try {
      await renderObjectTreeChildren(ref, container);
      setTreeBranchOpen(branch, wasOpen);
    } catch {
      // renderObjectTreeChildren already reports the error in the analysis panel.
    }
  }
}

function sectionTreeHtml(objectRef, section, data) {
  const structure = data.structure || {};
  const configName = data.identity?.config_name || '';
  const items = section.key === 'modules' ? objectGroupedModules(data) : (section.items || []);
  const tabularAttributes = structure.tabular_part_attributes || [];
  const itemsHtml = items.map((item) => {
    if (section.key === 'modules') {
      return moduleTreeItemHtml({ ...item, owner_qn: item.owner_qn || objectRef }, configName);
    }
    if (section.key === 'tabular_parts') {
      const attrs = tabularAttributes.filter((attr) => attr.parent_qualified_name === item.qualified_name);
      const attrsHtml = attrs.length ? `
        <div class="tree-branch tree-object-section-branch"
             data-ref="${escapeHtml(item.qualified_name || '')}"
             data-section="tabular_part_attributes">
          <div class="tree-row">
            <button class="tree-toggle" type="button" data-toggle="plain"
                    aria-label="Развернуть" title="Развернуть">+</button>
            <button class="tree-node tree-object-section-node" type="button"
                    data-kind="object-section"
                    data-ref="${escapeHtml(item.qualified_name || '')}"
                    data-section="tabular_part_attributes"
                    title="Реквизиты">
              ${treeIconHtml('attribute')}
              <span class="tree-label">Реквизиты</span>
              <span class="tree-count">${attrs.length.toLocaleString()}</span>
            </button>
          </div>
          <div class="tree-children" hidden>
            ${attrs.map((attr) => structureItemTreeHtml(attr, 'attributes', '', configName)).join('')}
          </div>
        </div>
      ` : '';
      return structureItemTreeHtml(item, section.key, attrsHtml, configName);
    }
    if (section.key === 'forms') {
      return formTreeItemHtml(data, item, configName);
    }
    if (section.key === 'commands') {
      const module = commandModuleForCommand(data, item.qualified_name);
      const moduleHtml = module
        ? moduleTreeItemHtml({ ...module, tree_label: moduleTreeLabel(module), owner_qn: module.owner_qn || item.qualified_name }, configName)
        : '';
      return structureItemTreeHtml(item, section.key, moduleHtml, configName);
    }
    return structureItemTreeHtml(item, section.key, '', configName);
  }).join('');

  return `
    <div class="tree-branch tree-object-section-branch"
         data-ref="${escapeHtml(objectRef)}"
         data-section="${escapeHtml(section.key)}">
      <div class="tree-row">
        <button class="tree-toggle" type="button" data-toggle="plain"
                aria-label="Развернуть" title="Развернуть">+</button>
        <button class="tree-node tree-object-section-node" type="button"
                data-kind="object-section"
                data-ref="${escapeHtml(objectRef)}"
                data-section="${escapeHtml(section.key)}"
                title="${escapeHtml(section.title)}">
          ${treeIconHtml(structureItemIcon(section.key))}
          <span class="tree-label">${escapeHtml(section.title)}</span>
          <span class="tree-count">${items.length.toLocaleString()}</span>
        </button>
      </div>
      <div class="tree-children" hidden>${itemsHtml}</div>
    </div>
  `;
}

async function renderObjectTreeChildren(ref, container) {
  if (container.dataset.loaded === '1') return;
  const nestedHtml = container.querySelector(':scope > .subsystem-children')?.outerHTML || '';
  container.innerHTML = `${nestedHtml}<div class="loading inline">Загрузка...</div>`;
  try {
    const data = await getObjectData(ref);
    const sections = objectStructureSections(data);
    const directModule = objectDirectModule(data);
    const directModuleHtml = directModule
      ? moduleTreeItemHtml({ ...directModule, tree_label: moduleTreeLabel(directModule), owner_qn: directModule.owner_qn || ref }, data.identity?.config_name || '')
      : '';
    const commonFormExtraHtml = await commonFormExtraSectionsHtml(ref, data);
    container.dataset.loaded = '1';
    if (!sections.length && !directModuleHtml && !commonFormExtraHtml) {
      if (nestedHtml) {
        container.innerHTML = nestedHtml;
        markActiveTreeNode();
        return;
      }
      container.innerHTML = '';
      const toggle = container.closest('.tree-object-branch')?.querySelector(':scope > .tree-row > .tree-toggle');
      if (toggle) {
        const spacer = document.createElement('span');
        spacer.className = 'tree-toggle-spacer';
        toggle.replaceWith(spacer);
      }
      return;
    }
    container.innerHTML = `${nestedHtml}${directModuleHtml}${commonFormExtraHtml}${sections.map((section) => sectionTreeHtml(ref, section, data)).join('')}`;
    markActiveTreeNode();
  } catch (err) {
    showAnalysisError(err.message);
    container.innerHTML = nestedHtml;
  }
}

function openAncestorBranches(node) {
  let branch = node?.closest('.tree-branch');
  while (branch) {
    const parentBranch = branch.parentElement?.closest('.tree-branch');
    if (parentBranch) setTreeBranchOpen(parentBranch, true);
    branch = parentBranch;
  }
}

async function expandTreePathForRef(ref, options = {}) {
  const parts = refPathParts(ref);
  if (parts.length < 4) return;
  const config = parts[1];
  const category = parts[2];
  const objectRef = parentObjectRef(ref);
  const configBranch = findConfigBranch(config);
  if (configBranch) setTreeBranchOpen(configBranch, true);

  await ensureCategoryLoadedUntilObject(config, category, objectRef);

  const objectBranch = findObjectBranch(objectRef);
  const objectContainer = objectBranch?.querySelector(':scope > .tree-children');
  if (objectBranch && objectContainer && parts.length > 4) {
    await renderObjectTreeChildren(objectRef, objectContainer);
    setTreeBranchOpen(objectBranch, true);
  }

  const exactNode = [...document.querySelectorAll('.tree-node[data-ref]')].find((node) => {
    return node.dataset.ref === ref;
  });
  if (exactNode) openAncestorBranches(exactNode);
  if (options.reveal === true) revealActiveTreeNode();
  else markActiveTreeNode();
}

function setDetailPanelMode(mode = '') {
  const detail = $('analysis-detail');
  const panel = detail?.closest('.detail-panel');
  const isModule = mode === 'module';
  if (panel) panel.classList.toggle('module-detail-mode', isModule);
  if (!isModule && analysisState.moduleCodeResizeAbort) {
    analysisState.moduleCodeResizeAbort.abort();
    analysisState.moduleCodeResizeAbort = null;
  }
}

function renderProjectDetail() {
  setDetailPanelMode('');
  const data = analysisState.tree;
  if (!data) return;
  clearNodeSelection('project');
  const configs = data.configurations || [];
  const objectCount = configs.reduce((sum, cfgItem) => {
    return sum + (cfgItem.categories || []).reduce((inner, cat) => inner + Number(cat.object_count || 0), 0);
  }, 0);
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-kicker">Проект</div>
        <h2>${escapeHtml(data.project?.name || '')}</h2>
      </div>
    </div>
    <div class="metric-row">
      ${metricHtml('Конфигурации', configs.length)}
      ${metricHtml('Объекты', objectCount)}
    </div>
    <div class="section-block">
      <h3>Конфигурации</h3>
      <div class="list-table">
        ${configs.map((config) => `
          <button class="row-button" type="button" data-kind="config" data-config="${escapeHtml(config.name || '')}">
            <span>${escapeHtml(config.name)}</span>
            <span>${config.is_extension ? 'Расширение' : 'Основная'}</span>
          </button>
        `).join('')}
      </div>
    </div>
  `;
}

function renderConfigDetail(configName) {
  setDetailPanelMode('');
  const config = (analysisState.tree?.configurations || []).find((item) => item.name === configName);
  if (!config) return;
  clearNodeSelection('config', configName);
  const categoryGroups = splitTreeCategories(config.categories);
  const categories = [...categoryGroups.common, ...categoryGroups.regular];
  const total = (config.categories || []).reduce((sum, cat) => sum + Number(cat.object_count || 0), 0);
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-kicker">${config.is_extension ? 'Расширение' : 'Конфигурация'}</div>
        <h2>${escapeHtml(config.name)}</h2>
        ${config.extends ? `<p class="muted">Расширяет: ${escapeHtml(config.extends.name)}</p>` : ''}
      </div>
    </div>
    <div class="metric-row">
      ${metricHtml('Категории', categories.length)}
      ${metricHtml('Объекты', total)}
    </div>
    <div class="section-block">
      <h3>Категории</h3>
      <div class="list-table">
        ${categories.map((cat) => `
          <button class="row-button" type="button" data-kind="category"
                  data-config="${escapeHtml(config.name)}"
                  data-category="${escapeHtml(cat.name)}">
            <span>${escapeHtml(categoryDisplayName(cat.name))}</span>
            <span>${Number(cat.object_count || 0).toLocaleString()}</span>
          </button>
        `).join('')}
      </div>
    </div>
  `;
}

function renderCategoryGroupDetail(configName, groupName) {
  setDetailPanelMode('');
  const config = (analysisState.tree?.configurations || []).find((item) => item.name === configName);
  if (!config || groupName !== 'common') return;
  clearGroupSelection(configName, groupName);
  const categories = splitTreeCategories(config.categories).common;
  const total = categories.reduce((sum, cat) => sum + Number(cat.object_count || 0), 0);
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-kicker">Группа категорий</div>
        <h2>Общие</h2>
        <p class="muted">${escapeHtml(configName)}</p>
      </div>
    </div>
    <div class="metric-row">
      ${metricHtml('Категории', categories.length)}
      ${metricHtml('Объекты', total)}
    </div>
    <div class="section-block">
      <h3>Категории</h3>
      <div class="list-table">
        ${categories.map((cat) => `
          <button class="row-button" type="button" data-kind="category"
                  data-config="${escapeHtml(configName)}"
                  data-category="${escapeHtml(cat.name)}">
            <span>${escapeHtml(categoryDisplayName(cat.name))}</span>
            <span>${Number(cat.object_count || 0).toLocaleString()}</span>
          </button>
        `).join('')}
      </div>
    </div>
  `;
}

async function selectCategory(button, options = {}) {
  const config = button.dataset.config;
  const category = button.dataset.category;
  const branch = button.closest('.tree-category') || findCategoryBranch(config, category);
  const container = branch?.querySelector('.category-objects');
  const key = categoryCacheKey(config, category);

  try {
    hideAnalysisError();
    if (!analysisState.categoryCache.has(key)) {
      if (container) container.innerHTML = '<div class="loading inline">Загрузка...</div>';
      const data = await fetchJson(withToken(`${API_PREFIX}/analysis/category`, { config, category, limit: 100, offset: 0 }));
      analysisState.categoryCache.set(key, data);
      if (container) renderCategoryObjects(container, data);
    } else if (container && container.dataset.loaded !== '1') {
      renderCategoryObjects(container, analysisState.categoryCache.get(key));
    }
    if (options.expand && branch) setTreeBranchOpen(branch, true);
    if (options.renderDetail !== false) renderCategoryDetail(analysisState.categoryCache.get(key));
  } catch (err) {
    showAnalysisError(err.message);
    if (container) container.innerHTML = '';
  }
}

function renderCategoryObjects(container, data) {
  const items = data.items || [];
  const nestedHtml = container.querySelector(':scope > .nested-categories')?.outerHTML || '';
  const tail = !data.all_loaded && data.total > items.length
    ? `
      <button class="tree-load-more" type="button" data-kind="load-more"
              data-config="${escapeHtml(data.config_name)}"
              data-category="${escapeHtml(data.category)}">
        Загрузить еще · ${items.length} из ${data.total}
      </button>
    `
    : '';
  container.dataset.loaded = '1';
  container.innerHTML = `
    ${nestedHtml}
    ${items.map((item) => objectTreeItemHtml(item, data, nestedSubsystemTreeHtml(item, data))).join('')}
    ${tail}
  `;
  markActiveTreeNode();
}

function nestedSubsystemTreeHtml(item, data) {
  const children = item.children || [];
  if (!children.length) return '';
  return children.map((child) => objectTreeItemHtml(child, data, nestedSubsystemTreeHtml(child, data))).join('');
}

function flattenCategoryItems(items) {
  const flat = [];
  const visit = (item) => {
    flat.push(item);
    (item.children || []).forEach(visit);
  };
  (items || []).forEach(visit);
  return flat;
}

function categoryDataContainsObject(data, objectRef) {
  if (!data || !objectRef) return false;
  return flattenCategoryItems(data.items || []).some((item) => item.qualified_name === objectRef);
}

async function ensureAnalysisTreeRenderedForNavigation() {
  const treeEl = $('analysis-tree');
  if (!analysisState.tree) {
    await loadAnalysisTree();
    return;
  }
  if (treeEl && !treeEl.querySelector('.tree-project')) {
    renderTree(analysisState.tree);
  }
}

async function ensureCategoryLoadedUntilObject(config, category, objectRef) {
  await ensureAnalysisTreeRenderedForNavigation();

  let branch = findCategoryBranch(config, category);
  if (!branch) return false;

  const categoryButton = branch.querySelector('.tree-node[data-kind="category"]');
  if (categoryButton) {
    await selectCategory(categoryButton, { expand: true, renderDetail: false });
  }

  const key = categoryCacheKey(config, category);
  let data = analysisState.categoryCache.get(key);
  let previousLength = -1;
  while (
    data
    && objectRef
    && !categoryDataContainsObject(data, objectRef)
    && Number((data.items || []).length) < Number(data.total || 0)
    && Number((data.items || []).length) !== previousLength
  ) {
    previousLength = Number((data.items || []).length);
    await loadMoreCategory(config, category);
    data = analysisState.categoryCache.get(key);
  }

  branch = findCategoryBranch(config, category);
  if (branch) setTreeBranchOpen(branch, true);
  return categoryDataContainsObject(data, objectRef);
}

function objectTreeItemHtml(item, data, nestedHtml = '') {
  const hasComposition = hasTreeComposition(item) || isCommonFormTreeItem(item, data);
  const hasNested = Boolean(nestedHtml);
  const hasChildren = hasComposition || hasNested;
  const iconState = extensionObjectState(item);
  const adoption = baseAdoptionMarkerHtml(item, data.config_name);
  return `
    <div class="tree-branch tree-object-branch"
         data-ref="${escapeHtml(item.qualified_name)}"
         data-config="${escapeHtml(data.config_name)}"
         data-category="${escapeHtml(data.category)}"
         data-has-composition="${hasComposition ? '1' : '0'}">
      <div class="tree-row">
        ${hasChildren ? `
          <button class="tree-toggle" type="button" data-toggle="object"
                   aria-label="Развернуть объект"
                   title="Развернуть">+</button>
        ` : '<span class="tree-toggle-spacer"></span>'}
        <button class="tree-node tree-object" type="button" data-kind="object"
                data-ref="${escapeHtml(item.qualified_name)}"
                title="${escapeHtml(item.name)}">
          ${treeIconWithStateHtml(categoryIconName(data.category), iconState)}
          <span class="tree-label">${escapeHtml(item.name)}</span>
          ${adoption}
        </button>
      </div>
      ${hasChildren ? `<div class="tree-children object-structure" hidden>${hasNested ? `<div class="subsystem-children">${nestedHtml}</div>` : ''}</div>` : ''}
    </div>
  `;
}

function renderCategoryDetail(data) {
  setDetailPanelMode('');
  if (!data) return;
  clearNodeSelection('category', data.config_name, data.category);
  const detailItems = data.all_loaded ? flattenCategoryItems(data.items || []) : (data.items || []);
  const shown = data.all_loaded ? detailItems.length : (data.items || []).length;
  const hasMore = !data.all_loaded && data.total > (data.items || []).length;
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-kicker">Категория</div>
        <h2>${escapeHtml(categoryDisplayName(data.category))}</h2>
        <p class="muted">${escapeHtml(data.config_name)}</p>
      </div>
    </div>
    <div class="metric-row">
      ${metricHtml('Объекты', data.total)}
      ${metricHtml('Показано', shown)}
    </div>
    <div class="object-list">
      ${detailItems.map(objectListItemHtml).join('')}
    </div>
    ${hasMore ? `
      <button class="load-more-button" type="button" data-kind="load-more"
              data-config="${escapeHtml(data.config_name)}"
              data-category="${escapeHtml(data.category)}">
        Загрузить еще · показано ${data.items.length} из ${data.total}
      </button>
    ` : ''}
  `;
}

async function loadMoreCategory(config, category) {
  const key = categoryCacheKey(config, category);
  const current = analysisState.categoryCache.get(key);
  if (!current) return;
  const offset = (current.items || []).length;
  if (offset >= current.total) return;

  const branch = findCategoryBranch(config, category);
  const container = branch?.querySelector('.category-objects');
  const buttons = [...document.querySelectorAll('button[data-kind="load-more"]')].filter((button) => {
    return button.dataset.config === config && button.dataset.category === category;
  });
  buttons.forEach((button) => {
    button.disabled = true;
    button.textContent = 'Загрузка...';
  });

  try {
    hideAnalysisError();
    const next = await fetchJson(withToken(`${API_PREFIX}/analysis/category`, {
      config,
      category,
      limit: current.limit || 100,
      offset,
    }));
    current.items = [...(current.items || []), ...(next.items || [])];
    current.total = next.total;
    current.limit = next.limit;
    current.offset = 0;
    analysisState.categoryCache.set(key, current);
    if (container) renderCategoryObjects(container, current);
    if (analysisState.selectedKind === 'category'
        && analysisState.selectedConfig === config
        && analysisState.selectedCategory === category) {
      renderCategoryDetail(current);
    }
  } catch (err) {
    showAnalysisError(err.message);
    if (container) renderCategoryObjects(container, current);
    if (analysisState.selectedKind === 'category'
        && analysisState.selectedConfig === config
        && analysisState.selectedCategory === category) {
      renderCategoryDetail(current);
    }
  }
}

function objectListItemHtml(item) {
  const counters = [
    ['Рекв.', item.attributes],
    ['ТЧ', item.tabular_parts],
    ['Формы', item.forms],
    ['Модули', item.modules],
    ['Движ.', item.movements],
  ].filter(([, value]) => Number(value || 0) > 0);

  return `
    <button class="object-card" type="button" data-ref="${escapeHtml(item.qualified_name)}">
      <span class="object-card-title">${escapeHtml(item.name)}</span>
      ${item.synonym ? `<span class="object-card-subtitle">${escapeHtml(item.synonym)}</span>` : ''}
      <span class="object-card-counters">
        ${counters.map(([label, value]) => `<span>${escapeHtml(label)} ${Number(value).toLocaleString()}</span>`).join('')}
      </span>
    </button>
  `;
}

async function selectObject(ref, options = {}) {
  if (!ref) return;
  if (options.pushBack !== false) pushBackSelection(ref);
  analysisState.selectedRef = ref;
  analysisState.selectedKind = 'object';
  analysisState.selectedConfig = null;
  analysisState.selectedCategory = null;
  analysisState.selectedSection = null;
  analysisState.selectedObject = null;
  analysisState.selectedNode = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = null;
  analysisState.selectedModuleOwner = null;
  analysisState.selectedModuleType = null;
  analysisState.relationships = null;
  analysisState.tab = normalizeObjectTab(options.tab || analysisState.tab);
  $('analysis-detail').innerHTML = '<div class="empty-state">Загрузка...</div>';
  setAnalysisUrlRef(ref);
  markActiveTreeNode();

  try {
    hideAnalysisError();
    const [objectData, relationships] = await Promise.all([
      getObjectData(ref),
      fetchJson(withToken(`${API_PREFIX}/analysis/relationships`, { ref, limit: 50, offset: 0 })),
    ]);
    analysisState.selectedObject = objectData;
    analysisState.relationships = relationships;
    if (isCommonModuleObject(objectData)) {
      try {
        analysisState.selectedModule = await getModuleData({
          ownerRef: ref,
          moduleType: 'CommonModule',
        });
      } catch (moduleErr) {
        console.error('failed to load common module code', moduleErr);
        analysisState.selectedModule = null;
      }
    }
    renderObjectDetail();
    setAnalysisUrlRef(ref);
    await expandTreePathForRef(ref, { reveal: options.revealInTree === true });
    ensureObjectVisibleInTree(objectData.identity);
  } catch (err) {
    if (err.message.startsWith('404:') || err.message.includes('object_not_found')) {
      await selectNode(ref, err.message, { pushBack: false });
      return;
    }
    showAnalysisError(err.message);
    $('analysis-detail').innerHTML = '<div class="empty-state">Объект не найден</div>';
  }
}

async function selectObjectSection(ref, section, options = {}) {
  if (!ref || !section) return;
  const objectRef = section === 'tabular_part_attributes' ? parentObjectRef(ref) : ref;
  if (options.pushBack !== false
      && (analysisState.selectedKind !== 'object-section'
      || analysisState.selectedRef !== ref
      || analysisState.selectedSection !== section)) {
    pushBackSelection(`${ref}#${section}`);
  }
  analysisState.selectedRef = ref;
  analysisState.selectedKind = 'object-section';
  analysisState.selectedConfig = null;
  analysisState.selectedCategory = null;
  analysisState.selectedSection = section;
  analysisState.selectedObject = null;
  analysisState.selectedNode = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = null;
  analysisState.selectedModuleOwner = null;
  analysisState.selectedModuleType = null;
  analysisState.relationships = null;
  $('analysis-detail').innerHTML = '<div class="empty-state">Загрузка...</div>';
  setAnalysisUrlRef(objectRef);
  markActiveTreeNode();

  try {
    hideAnalysisError();
    const data = await getObjectData(objectRef);
    analysisState.selectedObject = data;
    renderObjectSectionDetail(data, section, ref);
    await expandTreePathForRef(ref, { reveal: options.revealInTree === true });
  } catch (err) {
    showAnalysisError(err.message);
    $('analysis-detail').innerHTML = '<div class="empty-state">Раздел не найден</div>';
  }
}

async function selectNode(ref, fallbackMessage = '', options = {}) {
  if (!ref) return;
  if (options.pushBack !== false) pushBackSelection(ref);
  analysisState.selectedRef = ref;
  analysisState.selectedKind = 'node';
  analysisState.selectedConfig = null;
  analysisState.selectedCategory = null;
  analysisState.selectedSection = null;
  analysisState.selectedObject = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = null;
  analysisState.selectedModuleOwner = null;
  analysisState.selectedModuleType = null;
  analysisState.relationships = null;
  $('analysis-detail').innerHTML = '<div class="empty-state">Загрузка...</div>';
  setAnalysisUrlRef(ref);
  markActiveTreeNode();

  try {
    hideAnalysisError();
    const nodeData = await fetchJson(withToken(`${API_PREFIX}/analysis/node`, { ref }));
    const actualRef = nodeData?.node?.qualified_name || ref;
    analysisState.selectedRef = actualRef;
    analysisState.selectedNode = nodeData;
    setAnalysisUrlRef(actualRef);
    markActiveTreeNode();
    renderNodeDetail(nodeData);
    await expandTreePathForRef(actualRef, { reveal: options.revealInTree === true });
  } catch (err) {
    showAnalysisError(fallbackMessage || err.message);
    $('analysis-detail').innerHTML = '<div class="empty-state">Узел не найден</div>';
  }
}

async function selectModule({
  moduleId = '',
  ownerRef = '',
  moduleType = '',
  pushBack = true,
  revealInTree = false,
} = {}) {
  ({ moduleId, ownerRef, moduleType } = normalizeModuleRequest({ moduleId, ownerRef, moduleType }));
  const key = moduleSelectionKey(moduleId, ownerRef, moduleType);
  if (!key) return;
  if (pushBack !== false) pushBackSelection(key);
  analysisState.selectedKind = 'module';
  analysisState.selectedRef = null;
  analysisState.selectedConfig = null;
  analysisState.selectedCategory = null;
  analysisState.selectedSection = null;
  analysisState.selectedObject = null;
  analysisState.selectedNode = null;
  analysisState.selectedModule = null;
  analysisState.selectedModuleId = moduleId || null;
  analysisState.selectedModuleOwner = ownerRef || null;
  analysisState.selectedModuleType = moduleType || null;
  analysisState.relationships = null;
  $('analysis-detail').innerHTML = '<div class="empty-state">Загрузка...</div>';
  setAnalysisUrlRef('');
  markActiveTreeNode();

  try {
    hideAnalysisError();
    const data = await getModuleData({ moduleId, ownerRef, moduleType });
    analysisState.selectedModule = data;
    renderModuleDetail(data);
    const owner = data.identity?.owner_qn || ownerRef;
    if (owner) await expandTreePathForRef(owner);
    if (revealInTree) revealActiveTreeNode();
    saveAnalysisPageState();
  } catch (err) {
    showAnalysisError(err.message);
    $('analysis-detail').innerHTML = '<div class="empty-state">Модуль не найден</div>';
  }
}

function goBackSelection() {
  const previous = analysisState.backStack.pop();
  if (!previous) return;
  if (previous.type === 'object') {
    selectObject(previous.ref, { pushBack: false, tab: normalizeObjectTab(previous.tab) });
  } else if (previous.type === 'object-section') {
    selectObjectSection(previous.ref, previous.section, { pushBack: false });
  } else if (previous.type === 'module') {
    selectModule({
      moduleId: previous.moduleId,
      ownerRef: previous.ownerRef,
      moduleType: previous.moduleType,
      pushBack: false,
    });
  } else {
    selectNode(previous.ref, '', { pushBack: false });
  }
}

function setAnalysisUrlRef(ref) {
  const params = ref
    ? { ref, tab: analysisState.tab && analysisState.tab !== 'summary' ? analysisState.tab : '' }
    : {};
  const href = withToken(`${CONSOLE_PATH}/analysis`, params);
  window.history.replaceState({}, '', href);
  setupNavigation('analysis');
  saveAnalysisPageState();
}

function ensureObjectVisibleInTree(identity) {
  if (!identity?.qualified_name) return;
  const branch = findCategoryBranch(identity.config_name, identity.category);
  const container = branch?.querySelector('.category-objects');
  if (!container) return;
  const existing = [...container.querySelectorAll('.tree-object')].find((node) => {
    return node.dataset.ref === identity.qualified_name;
  });
  if (existing) {
    markActiveTreeNode();
  }
}

function renderObjectDetail() {
  setDetailPanelMode('');
  const data = analysisState.selectedObject;
  if (!data) return;
  const id = data.identity || {};
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-topline">
          ${renderBackButton()}
          <div class="detail-kicker">${escapeHtml(id.category || '')}</div>
        </div>
        <h2>${escapeHtml(id.name || '')}</h2>
        ${id.synonym ? `<p class="muted">${escapeHtml(id.synonym)}</p>` : ''}
        <p class="qn">${escapeHtml(id.qualified_name || '')}</p>
      </div>
      <div class="detail-actions">
        ${renderBadgesInline(data.badges)}
      </div>
    </div>
    <div class="tabs">
      ${getObjectTabs(data).map((tab) => tabButton(tab.key, tab.label)).join('')}
    </div>
    <div id="object-tab-body" class="tab-body"></div>
  `;
  renderObjectTab();
  markActiveTreeNode();
  const active = data?.object_summary?.action_state?.active_job;
  if (active && active.status === 'running') {
    // Poll on any running job, even for another object, so the "Занято Nс"
    // busy button on the current view unlocks automatically once the
    // foreign job terminates.
    objectSummaryController.startPolling(id.qualified_name);
  } else {
    objectSummaryController.stopAllTimers();
  }
}

function renderNodeDetail(data) {
  setDetailPanelMode('');
  const node = data.node || {};
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-topline">
          ${renderBackButton()}
          <div class="detail-kicker">${escapeHtml(nodeLabel(node.label))}</div>
        </div>
        <h2>${escapeHtml(node.name || '')}</h2>
        <p class="qn">${escapeHtml(node.qualified_name || '')}</p>
      </div>
      <div class="detail-actions">
        ${renderBadgesInline(data.badges)}
      </div>
    </div>
    <div class="tab-body">
      ${renderProperties(data.properties || [])}
      ${renderNodeEvents(data.events || [])}
    </div>
  `;
  markActiveTreeNode();
}

function renderModuleDetail(data) {
  setDetailPanelMode('module');
  const identity = data.identity || {};
  const routines = data.routines || [];
  const exported = routines.filter((routine) => routine.export).length;
  const code = data.code || '';
  const properties = data.properties || [];
  $('analysis-detail').innerHTML = `
    <div class="module-detail-view">
      <div class="module-detail-overview">
        <div class="detail-title-row">
          <div>
            <div class="detail-topline">
              ${renderBackButton()}
              <div class="detail-kicker">Модуль</div>
            </div>
            <h2>${escapeHtml(identity.name || 'Модуль')}</h2>
            ${identity.owner_qn ? `<p class="qn">${escapeHtml(identity.owner_qn)}</p>` : ''}
          </div>
          <div class="detail-actions">
            ${renderBadgesInline(data.badges)}
          </div>
        </div>
        <div class="metric-row compact">
          ${metricHtml('Процедуры', routines.length)}
          ${metricHtml('Экспортные', exported)}
        </div>
        <div class="section-block">
          <h3>Основные сведения</h3>
          <dl class="property-list">
            ${definitionHtml('Тип модуля', moduleTypeLabel(identity.module_type))}
            ${definitionHtml('Конфигурация', identity.config_name)}
            ${definitionHtml('Путь', identity.path)}
          </dl>
        </div>
      </div>
      ${renderModuleCodeBlock(code)}
      ${properties.length ? `<div class="module-detail-tail">${renderProperties(properties)}</div>` : ''}
    </div>
  `;
  enhanceModuleCodeViewer();
  markActiveTreeNode();
}

function renderModuleCodeInfo() {
  return `
    <span class="module-code-info">
      <button class="module-code-info-trigger" type="button" aria-label="О реконструированном коде">i</button>
      <span class="module-code-info-tooltip" role="tooltip">
        <strong>Реконструированный код</strong>
        <span>Текст собран из процедур и функций, сохраненных в базе данных, а не прочитан целиком из исходного файла модуля.</span>
        <span>Может отсутствовать:</span>
        <ul>
          <li>объявления переменных модуля;</li>
          <li>директивы и условная компиляция вне процедур/функций;</li>
          <li>#Область / #КонецОбласти как исходная структура файла;</li>
          <li>комментарии между процедурами/функциями и шапка файла;</li>
          <li>точное исходное форматирование между routines;</li>
          <li>процедуры/функции, которые не удалось распознать при разборе.</li>
        </ul>
        <span>Комментарии и директивы внутри найденных процедур/функций сохраняются. Порядок обычно восстановлен по файлу и номеру строки.</span>
      </span>
    </span>
  `;
}

function renderModuleCodeBlock(code) {
  return `
    <div class="section-block module-code-section">
      <div class="module-code-heading">
        <h3>Код</h3>
        ${renderModuleCodeInfo()}
      </div>
      ${code ? `
        <div class="module-code-toolbar">
          <button class="secondary-button module-code-button" type="button" data-module-code-action="collapse">
            <span class="module-code-button-icon" aria-hidden="true">⊟</span>
            <span>Свернуть все</span>
          </button>
          <button class="secondary-button module-code-button" type="button" data-module-code-action="expand">
            <span class="module-code-button-icon" aria-hidden="true">⊞</span>
            <span>Развернуть все</span>
          </button>
          <button class="secondary-button module-code-button" type="button" data-module-code-action="units" aria-pressed="${analysisState.moduleUnitsVisible ? 'true' : 'false'}" title="Показать юниты кода">
            <span class="module-code-button-icon" aria-hidden="true">⋮</span>
            <span>Юниты кода</span>
          </button>
          <div class="module-code-search">
            <input type="search" data-role="module-code-search" placeholder="Поиск по коду">
            <span class="module-code-search-count" data-role="module-code-search-count"></span>
            <button class="panel-icon-button" type="button" data-module-code-action="prev" title="Предыдущее совпадение">↑</button>
            <button class="panel-icon-button" type="button" data-module-code-action="next" title="Следующее совпадение">↓</button>
          </div>
        </div>
        <div class="module-code-viewer" data-role="module-code-viewer">
          <div class="module-code-units-overlay" data-role="module-code-units-overlay" hidden></div>
          <pre><code class="lang-1c">${escapeHtml(code)}</code></pre>
        </div>
      ` : '<div class="empty-state compact">Код модуля не загружен</div>'}
    </div>
  `;
}

function enhanceModuleCodeViewer() {
  const viewer = document.querySelector('[data-role="module-code-viewer"]');
  if (!viewer) return;
  if (window.BSL && typeof window.BSL.highlightAll === 'function') {
    try {
      window.BSL.highlightAll(viewer, { autodetect: false, inline: false });
    } catch (err) {
      console.error('BSL highlighting error:', err);
    }
  }
  if (typeof CodeFoldingManager !== 'undefined') {
    try {
      const manager = new CodeFoldingManager(viewer, () => {
        if (analysisState.moduleUnitsVisible) requestModuleCodeUnitsRender(viewer);
      });
      const blocks = manager.parseProceduresAndFunctions();
      if (blocks.length) {
        manager.injectFoldIndicators();
        manager.attachFoldEventListeners();
        manager.collapseAll();
        viewer._codeFoldingManager = manager;
      }
    } catch (err) {
      console.error('BSL folding error:', err);
    }
  }
  bindModuleCodeControls(viewer);
  setupModuleCodeViewerResize(viewer);
  if (analysisState.moduleUnitsVisible) {
    requestModuleCodeUnitsRender(viewer);
  }
}

function setupModuleCodeViewerResize(viewer) {
  const panel = viewer.closest('.module-detail-view') || viewer.closest('.detail-panel');
  const section = viewer.closest('.module-code-section');
  if (!panel || !section) return;
  if (analysisState.moduleCodeResizeAbort) {
    analysisState.moduleCodeResizeAbort.abort();
  }
  const controller = new AbortController();
  analysisState.moduleCodeResizeAbort = controller;
  const pinnedScrollTop = moduleCodePinnedScrollTop(section);
  const resize = () => {
    if (!viewer.isConnected) {
      controller.abort();
      return;
    }
    const panelRect = panel.getBoundingClientRect();
    section.style.top = '0px';
    const viewerRect = viewer.getBoundingClientRect();
    const bottomPadding = 16;
    const available = Math.floor(panelRect.bottom - viewerRect.top - bottomPadding);
    const minHeight = 360;
    if (available >= minHeight) {
      viewer.style.height = `${available}px`;
    } else {
      viewer.style.height = '';
    }
  };
  panel.addEventListener('scroll', resize, { passive: true, signal: controller.signal });
  window.addEventListener('resize', resize, { signal: controller.signal });
  viewer.addEventListener('wheel', (event) => {
    const targetScrollTop = pinnedScrollTop;
    if (event.deltaY > 0 && panel.scrollTop < targetScrollTop - 1) {
      event.preventDefault();
      const nextPanelScrollTop = panel.scrollTop + event.deltaY;
      panel.scrollTop = Math.min(targetScrollTop, nextPanelScrollTop);
      if (nextPanelScrollTop > targetScrollTop) {
        viewer.scrollTop += nextPanelScrollTop - targetScrollTop;
      }
      resize();
      return;
    }
    if (event.deltaY < 0 && viewer.scrollTop <= 0 && panel.scrollTop > 0) {
      event.preventDefault();
      panel.scrollTop = Math.max(0, panel.scrollTop + event.deltaY);
      resize();
    }
  }, { passive: false, signal: controller.signal });
  requestAnimationFrame(resize);
}

function moduleCodePinnedScrollTop(section) {
  return Math.max(0, section.offsetTop);
}

function bindModuleCodeControls(viewer) {
  const block = viewer.closest('.section-block');
  if (!block || block.dataset.moduleCodeControlsBound === '1') return;
  block.dataset.moduleCodeControlsBound = '1';
  const searchInput = block.querySelector('[data-role="module-code-search"]');
  const countEl = block.querySelector('[data-role="module-code-search-count"]');
  const runSearch = () => updateModuleCodeSearch(viewer, searchInput?.value || '', countEl);

  block.addEventListener('click', (event) => {
    const action = event.target.closest('[data-module-code-action]')?.dataset.moduleCodeAction;
    if (!action) return;
    if (action === 'collapse') {
      clearModuleCodeSearch(viewer, countEl);
      if (searchInput) searchInput.value = '';
      viewer._codeFoldingManager?.collapseAll();
      if (analysisState.moduleUnitsVisible) requestModuleCodeUnitsRender(viewer);
    } else if (action === 'expand') {
      viewer._codeFoldingManager?.expandAll();
      if (analysisState.moduleUnitsVisible) requestModuleCodeUnitsRender(viewer);
    } else if (action === 'units') {
      setModuleUnitsVisible(viewer, !analysisState.moduleUnitsVisible);
    } else if (action === 'prev') {
      navigateModuleCodeSearch(viewer, -1, countEl);
    } else if (action === 'next') {
      navigateModuleCodeSearch(viewer, 1, countEl);
    }
  });

  if (searchInput) {
    searchInput.addEventListener('input', runSearch);
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        navigateModuleCodeSearch(viewer, event.shiftKey ? -1 : 1, countEl);
      }
    });
  }
}

function setModuleUnitsVisible(viewer, visible) {
  analysisState.moduleUnitsVisible = Boolean(visible);
  const button = viewer.closest('.section-block')?.querySelector('[data-module-code-action="units"]');
  if (button) button.setAttribute('aria-pressed', analysisState.moduleUnitsVisible ? 'true' : 'false');
  if (!analysisState.moduleUnitsVisible) {
    clearModuleCodeUnitsOverlay(viewer);
    return;
  }
  requestModuleCodeUnitsRender(viewer);
}

function clearModuleCodeUnitsOverlay(viewer) {
  const overlay = viewer.querySelector('[data-role="module-code-units-overlay"]');
  if (!overlay) return;
  overlay.hidden = true;
  overlay.innerHTML = '';
  overlay.style.height = '';
}

function requestModuleCodeUnitsRender(viewer) {
  const request = currentModuleRequest();
  const key = moduleSelectionKey(request.moduleId, request.ownerRef, request.moduleType);
  if (!key) return;
  const sequence = (viewer._moduleUnitsSequence || 0) + 1;
  viewer._moduleUnitsSequence = sequence;
  requestAnimationFrame(async () => {
    try {
      const data = await getModuleUnitsData(request);
      if (viewer._moduleUnitsSequence !== sequence || !analysisState.moduleUnitsVisible) return;
      renderModuleCodeUnitsOverlay(viewer, data);
    } catch (err) {
      console.error('module code units error:', err);
      clearModuleCodeUnitsOverlay(viewer);
    }
  });
}

function renderModuleCodeUnitsOverlay(viewer, data) {
  const overlay = viewer.querySelector('[data-role="module-code-units-overlay"]');
  const pre = viewer.querySelector('pre');
  if (!overlay || !pre || !data?.available || !Number(data.unit_count || 0)) {
    clearModuleCodeUnitsOverlay(viewer);
    return;
  }
  const preStyle = getComputedStyle(pre);
  const fontSize = parseFloat(preStyle.fontSize) || 13;
  const lineHeight = parseFloat(preStyle.lineHeight) || fontSize * 1.55;
  const paddingTop = parseFloat(preStyle.paddingTop) || 0;
  overlay.innerHTML = '';
  overlay.hidden = false;
  overlay.style.height = `${Math.max(pre.scrollHeight, viewer.scrollHeight)}px`;
  const collapsedRanges = moduleCodeCollapsedRanges(viewer);

  const routines = Array.isArray(data.routines) ? data.routines : [];
  routines.forEach((routine) => {
    const units = Array.isArray(routine.units) ? routine.units : [];
    units.forEach((unit) => {
      const start = Number(unit.display_line_start || 0);
      const end = Math.max(start, Number(unit.display_line_end || start));
      if (!start || !end) return;
      const visibleStart = firstVisibleModuleCodeLine(start, end, collapsedRanges);
      const visibleEnd = lastVisibleModuleCodeLine(start, end, collapsedRanges);
      if (!visibleStart || !visibleEnd) return;
      const visualStart = visualModuleCodeLine(visibleStart, collapsedRanges);
      const visualEnd = visualModuleCodeLine(visibleEnd, collapsedRanges);
      if (!visualStart || !visualEnd) return;
      const lane = Math.max(0, Number(unit.lane || 0));
      const partIndex = Math.max(0, Number(unit.part_index || 0));
      const displayPartIndex = partIndex + 1;
      const color = MODULE_UNIT_COLORS[partIndex % MODULE_UNIT_COLORS.length];
      const range = document.createElement('span');
      range.className = 'module-code-unit-range';
      range.style.setProperty('--unit-color', color);
      range.style.left = `${28 + lane * 6}px`;
      range.style.top = `${paddingTop + (visualStart - 1) * lineHeight}px`;
      range.style.height = `${Math.max(lineHeight, (visualEnd - visualStart + 1) * lineHeight)}px`;
      const title = [
        `${routine.name || 'Процедура'}: юнит ${displayPartIndex} из ${unit.part_total || '?'}`,
        `Строки модуля: ${start}-${end}`,
        `Строки тела: ${unit.line_start || '?'}-${unit.line_end || '?'}`,
      ];
      const charCount = moduleCodeUnitCharCount(unit);
      if (charCount !== null) title.push(`Символов: ${formatRuNumber(charCount)}`);
      if (visibleStart !== start || visibleEnd !== end) {
        title.push('Часть диапазона скрыта свернутым блоком');
      }
      range.title = title.join('\n');
      range.innerHTML = '<span class="module-code-unit-dot start"></span><span class="module-code-unit-line"></span><span class="module-code-unit-dot end"></span>';
      overlay.appendChild(range);
    });
  });
  if (!overlay.children.length) clearModuleCodeUnitsOverlay(viewer);
}

function moduleCodeUnitCharCount(unit) {
  const explicit = Number(unit?.size_chars);
  if (Number.isFinite(explicit) && explicit >= 0) return Math.round(explicit);
  const start = Number(unit?.char_start);
  const end = Number(unit?.char_end);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return Math.round(end - start);
}

function moduleCodeCollapsedRanges(viewer) {
  const blocks = viewer._codeFoldingManager?.foldableBlocks || [];
  const ranges = blocks
    .filter((block) => block?.collapsed && Number.isFinite(Number(block.collapsedEndLine)))
    .map((block) => ({
      start: Number(block.startLine) + 2,
      end: Number(block.collapsedEndLine) + 1,
    }))
    .filter((range) => range.start <= range.end)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const merged = [];
  ranges.forEach((range) => {
    const previous = merged[merged.length - 1];
    if (previous && range.start <= previous.end + 1) {
      previous.end = Math.max(previous.end, range.end);
    } else {
      merged.push({ ...range });
    }
  });
  return merged;
}

function firstVisibleModuleCodeLine(start, end, collapsedRanges) {
  let line = start;
  for (const range of collapsedRanges) {
    if (line > end) return null;
    if (range.end < line) continue;
    if (range.start > end) break;
    if (line < range.start) return line;
    line = range.end + 1;
  }
  return line <= end ? line : null;
}

function lastVisibleModuleCodeLine(start, end, collapsedRanges) {
  let line = end;
  for (let index = collapsedRanges.length - 1; index >= 0; index -= 1) {
    const range = collapsedRanges[index];
    if (line < start) return null;
    if (range.start > line) continue;
    if (range.end < start) break;
    if (line > range.end) return line;
    line = range.start - 1;
  }
  return line >= start ? line : null;
}

function visualModuleCodeLine(line, collapsedRanges) {
  let hiddenBefore = 0;
  for (const range of collapsedRanges) {
    if (range.end < line) {
      hiddenBefore += range.end - range.start + 1;
    } else if (range.start <= line) {
      return null;
    } else {
      break;
    }
  }
  return line - hiddenBefore;
}

function moduleCodeHighlightSupported() {
  return Boolean(window.CSS && CSS.highlights && typeof Highlight !== 'undefined');
}

function moduleCodeSearchState(viewer) {
  if (!viewer._moduleCodeSearch) {
    viewer._moduleCodeSearch = {
      ranges: [],
      current: -1,
    };
  }
  return viewer._moduleCodeSearch;
}

function clearModuleCodeSearch(viewer, countEl) {
  if (moduleCodeHighlightSupported()) {
    CSS.highlights.delete('module-code-search');
    CSS.highlights.delete('module-code-current');
  }
  const state = moduleCodeSearchState(viewer);
  state.ranges = [];
  state.current = -1;
  if (countEl) countEl.textContent = '';
}

function updateModuleCodeSearch(viewer, query, countEl) {
  clearModuleCodeSearch(viewer, countEl);
  const needle = String(query || '').trim();
  if (!needle) return;

  viewer._codeFoldingManager?.expandAll();
  const code = viewer.querySelector('code');
  if (!code) return;
  const state = moduleCodeSearchState(viewer);
  const ranges = collectModuleCodeSearchRanges(code, needle);
  state.ranges = ranges;
  state.current = ranges.length ? 0 : -1;

  if (moduleCodeHighlightSupported() && ranges.length) {
    CSS.highlights.set('module-code-search', new Highlight(...ranges));
  }
  updateModuleCodeCurrentSearch(viewer, countEl);
}

function collectModuleCodeSearchRanges(root, query) {
  const ranges = [];
  const needle = query.toLocaleLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    const text = node.nodeValue || '';
    const lower = text.toLocaleLowerCase();
    let from = 0;
    while (needle && from < lower.length) {
      const index = lower.indexOf(needle, from);
      if (index === -1) break;
      const range = document.createRange();
      range.setStart(node, index);
      range.setEnd(node, index + query.length);
      ranges.push(range);
      from = index + Math.max(query.length, 1);
    }
    node = walker.nextNode();
  }
  return ranges;
}

function navigateModuleCodeSearch(viewer, direction, countEl) {
  const state = moduleCodeSearchState(viewer);
  if (!state.ranges.length) return;
  state.current = (state.current + direction + state.ranges.length) % state.ranges.length;
  updateModuleCodeCurrentSearch(viewer, countEl);
}

function updateModuleCodeCurrentSearch(viewer, countEl) {
  const state = moduleCodeSearchState(viewer);
  if (!state.ranges.length) {
    if (countEl) countEl.textContent = 'Не найдено';
    if (moduleCodeHighlightSupported()) CSS.highlights.delete('module-code-current');
    return;
  }
  if (countEl) countEl.textContent = `${state.current + 1} из ${state.ranges.length}`;
  const currentRange = state.ranges[state.current];
  if (moduleCodeHighlightSupported()) {
    CSS.highlights.set('module-code-current', new Highlight(currentRange));
  }
  scrollModuleCodeRangeIntoView(viewer, currentRange);
}

function scrollModuleCodeRangeIntoView(viewer, range) {
  const rect = range?.getBoundingClientRect();
  if (!rect || rect.height === 0) return;
  const viewerRect = viewer.getBoundingClientRect();
  if (rect.top < viewerRect.top + 24 || rect.bottom > viewerRect.bottom - 24) {
    viewer.scrollTop += rect.top - viewerRect.top - 80;
  }
}

function tabButton(tab, label) {
  const active = analysisState.tab === tab ? ' active' : '';
  return `<button class="tab-button${active}" type="button" data-tab="${tab}">${escapeHtml(label)}</button>`;
}

function getObjectTabs(data) {
  const tabs = [{ key: 'summary', label: 'Сведения' }];
  const relationships = getVisibleRelationshipGroups();
  const hasRoleGrants = relationships.some((group) => group.key === 'role_grants');
  const hasOtherRelationships = relationships.some((group) => group.key !== 'role_grants');

  if (hasRoleGrants) tabs.push({ key: 'access', label: 'Права' });
  if (hasOtherRelationships) tabs.push({ key: 'relationships', label: 'Связи' });
  if ((data.properties || []).length > 0) tabs.push({ key: 'properties', label: 'Свойства' });
  return tabs;
}

function ensureObjectTab(data) {
  analysisState.tab = normalizeObjectTab(analysisState.tab);
  const tabs = getObjectTabs(data);
  if (!tabs.some((tab) => tab.key === analysisState.tab)) {
    analysisState.tab = tabs[0]?.key || 'summary';
  }
}

function renderObjectTab() {
  const body = $('object-tab-body');
  const data = analysisState.selectedObject;
  if (!body || !data) return;
  ensureObjectTab(data);

  $('analysis-detail').querySelectorAll('.tab-button').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === analysisState.tab);
  });

  if (analysisState.tab === 'summary') body.innerHTML = renderSummaryTab(data);
  if (analysisState.tab === 'access') body.innerHTML = renderAccessTab();
  if (analysisState.tab === 'relationships') body.innerHTML = renderRelationshipsTab();
  if (analysisState.tab === 'properties') body.innerHTML = renderProperties(data.properties || []);
  if (analysisState.tab === 'summary' && isCommonModuleObject(data)) {
    enhanceModuleCodeViewer();
  }
}

function isCommonModuleObject(data) {
  const identity = data?.identity || {};
  return String(identity.category || '').toLowerCase() === 'общиемодули';
}

function renderSummaryTab(data) {
  const identity = data.identity || {};
  const commonModule = isCommonModuleObject(data);
  const moduleCode = commonModule ? (analysisState.selectedModule?.code || '') : '';
  const metrics = Object.entries(data.counters || {})
    .filter(([, value]) => Number(value || 0) > 0)
    .map(([key, value]) => metricHtml(counterLabel(key), value))
    .join('');
  return `
    ${metrics ? `<div class="metric-row compact">${metrics}</div>` : ''}
    <div class="section-block">
      <h3>Основные сведения</h3>
      <dl class="property-list">
        ${definitionHtml('Конфигурация', identity.config_name)}
        ${definitionHtml('Категория', identity.category)}
        ${definitionHtml('Синоним', identity.synonym)}
        ${definitionHtml('Комментарий', identity.comment)}
        ${definitionHtml('Пояснение', identity.explanation)}
      </dl>
    </div>
    ${commonModule ? renderModuleCodeBlock(moduleCode) : renderObjectSummaryBlock(data.object_summary)}
  `;
}

function renderObjectSummaryBlock(summaryState) {
  const actionState = summaryState?.action_state || null;
  const buttonHtml = renderSummaryActionButton(actionState);
  if (!summaryState?.available) {
    const meta = '';
    return `
      <div class="section-block object-summary">
        <div class="summary-heading">
          <h3>Сводка</h3>
          ${meta}
          ${buttonHtml}
        </div>
        <p class="muted">Сводка по объекту метаданных пока не сформирована.</p>
      </div>
    `;
  }
  const summary = summaryState.human_summary || {};
  const parts = [
    summaryTextSection('Назначение', summary.core_idea),
    summaryTextSection('Состав данных', summary.data_scope),
    summaryItemsSection('Возможности', summary.capabilities),
    summaryItemsSection('Сценарии использования', summary.usage_scenarios),
    summaryTextSection('Результат', summary.effects),
    summaryTextSection('Ограничения', summary.uncertainties),
  ].filter(Boolean).join('');

  return `
    <div class="section-block object-summary">
      <div class="summary-heading">
        <h3>Сводка</h3>
        ${renderObjectSummaryMeta(summaryState.meta)}
        ${buttonHtml}
      </div>
      ${parts || '<p class="muted">Сводка по объекту метаданных пока пустая.</p>'}
    </div>
  `;
}

function renderSummaryActionButton(actionState) {
  if (!actionState) return '';
  const active = actionState.active_job;
  const ref = analysisState.selectedRef || '';
  const enabled = !!actionState.enabled;
  const startupReady = !!actionState.startup_ready;
  const eligible = !!actionState.eligible;
  const canCreate = !!actionState.can_create;
  const canUpdate = !!actionState.can_update;

  const isRunningActive = !!(active && active.status === 'running');

  if (isRunningActive && active.qualified_name === ref) {
    const verb = active.action === 'create' ? 'Создание' : 'Обновление';
    return summaryButtonHtml({
      label: `${verb} ${active.elapsed_seconds || 0}с`,
      disabled: true,
      action: active.action,
      startedAt: active.started_at || '',
      ownRef: ref,
    });
  }
  if (isRunningActive && active.qualified_name !== ref) {
    return summaryButtonHtml({
      label: `Занято ${active.elapsed_seconds || 0}с`,
      disabled: true,
      action: 'busy',
      startedAt: active.started_at || '',
      ownRef: '',
    });
  }
  if (!IS_ADMIN) return '';
  if (!enabled) return '';
  if (!eligible) return '';
  if (!startupReady) {
    return summaryButtonHtml({
      label: 'Подготовка...',
      disabled: true,
      action: 'preparing',
      startedAt: '',
      ownRef: ref,
    });
  }
  if (!actionState.has_summary && canCreate) {
    return summaryButtonHtml({
      label: 'Создать',
      disabled: false,
      action: 'create',
      startedAt: '',
      ownRef: ref,
    });
  }
  if (actionState.has_summary && canUpdate) {
    return summaryButtonHtml({
      label: 'Обновить',
      disabled: false,
      action: 'refresh',
      startedAt: '',
      ownRef: ref,
    });
  }
  return '';
}

function summaryButtonHtml({ label, disabled, action, startedAt, ownRef }) {
  return `<button class="summary-action-button" type="button" `
    + `data-summary-action="${escapeHtml(action)}" `
    + `data-summary-started-at="${escapeHtml(startedAt || '')}" `
    + `data-summary-ref="${escapeHtml(ownRef || '')}"`
    + `${disabled ? ' disabled' : ''}>`
    + `${escapeHtml(label)}</button>`;
}

function renderObjectSummaryMeta(meta) {
  const text = formatObjectSummaryMeta(meta);
  if (!text) return '';
  return `<div class="summary-meta" title="${escapeHtml(text)}">${escapeHtml(text)}</div>`;
}

function formatObjectSummaryMeta(meta) {
  if (!meta || typeof meta !== 'object') return '';
  const parts = [];
  const generatedAt = formatSummaryDate(meta.generated_at);
  if (generatedAt) parts.push(generatedAt);
  const model = String(meta.model || '').trim();
  if (model) parts.push(model);
  const inputTokens = formatSummaryNumber(meta.input_tokens);
  const outputTokens = formatSummaryNumber(meta.output_tokens);
  if (inputTokens && outputTokens) {
    parts.push(`${inputTokens} → ${outputTokens}`);
  } else if (inputTokens) {
    parts.push(inputTokens);
  }
  const cost = formatSummaryCost(meta.cost_amount, meta.cost_unit);
  if (cost) parts.push(cost);
  return parts.join(' · ');
}

function formatSummaryNumber(value) {
  if (value === null || value === undefined || value === '') return '';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '';
  return numeric.toLocaleString('ru-RU');
}

function formatSummaryDate(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return raw;
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date).replace(',', '');
}

function formatSummaryCost(amount, unit) {
  if (amount === null || amount === undefined || amount === '') return '';
  const numeric = Number(amount);
  if (!Number.isFinite(numeric)) return '';
  const unitText = String(unit || '').trim().toLowerCase();
  const formatted = numeric < 0.01 ? numeric.toFixed(6) : numeric.toFixed(3);
  if (!unitText || unitText === 'usd' || unitText === 'dollar' || unitText === 'dollars') {
    return `$${formatted}`;
  }
  return `${formatted} ${unitText}`;
}

function summaryTextSection(title, value) {
  const text = String(value || '').trim();
  if (!text) return '';
  return `
    <section class="summary-section">
      <h4>${escapeHtml(title)}</h4>
      <p>${escapeHtml(text)}</p>
    </section>
  `;
}

function summaryItemsSection(title, items) {
  const rows = (Array.isArray(items) ? items : [])
    .map((item) => {
      if (!item || typeof item !== 'object') return '';
      const itemTitle = String(item.title || '').trim();
      const description = String(item.description || '').trim();
      if (!itemTitle && !description) return '';
      return `
        <li>
          ${itemTitle ? `<div class="summary-item-title">${escapeHtml(itemTitle)}</div>` : ''}
          ${description ? `<div class="summary-item-text">${escapeHtml(description)}</div>` : ''}
        </li>
      `;
    })
    .filter(Boolean)
    .join('');
  if (!rows) return '';
  return `
    <section class="summary-section">
      <h4>${escapeHtml(title)}</h4>
      <ul>${rows}</ul>
    </section>
  `;
}

// =====================================================================
// Object summary manual controller (button → POST run → polling + 1s tick).
// =====================================================================

const objectSummaryController = (() => {
  let pollTimer = null;
  let tickTimer = null;
  let pollingRef = null;
  let pendingJobId = null;
  let pendingAction = null;
  let pendingStartedAt = null;
  let confirmModal = null;
  let activeConfirmHandler = null;
  let activeConfirmBtn = null;

  function attach() {
    document.addEventListener('click', (event) => {
      const btn = event.target.closest('.summary-action-button');
      if (!btn) return;
      if (btn.closest('#summary-refresh-modal')) return; // modal buttons handled separately
      const action = btn.dataset.summaryAction;
      const ref = btn.dataset.summaryRef || analysisState.selectedRef;
      if (!ref) return;
      if (action === 'create') {
        startJob(ref, 'create');
      } else if (action === 'refresh') {
        confirmRefresh(ref);
      }
    });
  }

  function detachConfirmHandler() {
    if (activeConfirmHandler && activeConfirmBtn) {
      activeConfirmBtn.removeEventListener('click', activeConfirmHandler);
    }
    activeConfirmHandler = null;
    activeConfirmBtn = null;
  }

  function closeConfirmModal() {
    detachConfirmHandler();
    if (confirmModal) confirmModal.hidden = true;
  }

  function ensureConfirmModal() {
    if (confirmModal) return confirmModal;
    const modal = document.createElement('div');
    modal.id = 'summary-refresh-modal';
    modal.className = 'condition-modal';
    modal.hidden = true;
    modal.innerHTML = `
      <div class="condition-modal-backdrop" data-role="cancel"></div>
      <section class="condition-modal-dialog summary-refresh-dialog" role="dialog" aria-modal="true" aria-labelledby="summary-refresh-title">
        <header class="condition-modal-header">
          <div>
            <h3 id="summary-refresh-title">Обновить сводку?</h3>
          </div>
          <button class="panel-icon-button" type="button" data-role="cancel" title="Закрыть">×</button>
        </header>
        <p class="condition-modal-text summary-refresh-text">Текущая сводка будет сохранена в архиве, затем будет создана новая.</p>
        <footer class="condition-modal-footer summary-refresh-actions">
          <button class="summary-action-button" type="button" data-role="confirm">Обновить</button>
          <button class="summary-action-button summary-secondary-button" type="button" data-role="cancel">Отмена</button>
        </footer>
      </section>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', (event) => {
      if (event.target.closest('[data-role="cancel"]')) closeConfirmModal();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && !modal.hidden) closeConfirmModal();
    });
    confirmModal = modal;
    return modal;
  }

  function confirmRefresh(ref) {
    const modal = ensureConfirmModal();
    const confirmBtn = modal.querySelector('[data-role="confirm"]');
    // Detach any previously bound handler from a prior (cancelled) open.
    detachConfirmHandler();
    const handler = () => {
      detachConfirmHandler();
      modal.hidden = true;
      startJob(ref, 'refresh');
    };
    activeConfirmHandler = handler;
    activeConfirmBtn = confirmBtn;
    confirmBtn.addEventListener('click', handler);
    modal.hidden = false;
  }

  function applyLocalRunningState(ref, action) {
    // Immediately reflect the new job in the existing DOM so the user
    // sees a disabled "Создание/Обновление 0с" button before the first
    // backend poll arrives.
    pendingAction = action;
    pendingStartedAt = new Date().toISOString();
    const btn = document.querySelector('.summary-action-button');
    if (btn) {
      const verb = action === 'create' ? 'Создание' : 'Обновление';
      btn.textContent = `${verb} 0с`;
      btn.dataset.summaryAction = action;
      btn.dataset.summaryStartedAt = pendingStartedAt;
      btn.dataset.summaryRef = ref;
      btn.disabled = true;
    }
  }

  async function startJob(ref, action) {
    const url = withToken(`${API_PREFIX}/analysis/object-summary/run`);
    let res;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ref, action }),
      });
    } catch (err) {
      console.error('summary run failed', err);
      alert('Сеть недоступна, попробуйте ещё раз.');
      return;
    }
    if (res.status !== 202) {
      let payload = {};
      try { payload = await res.json(); } catch (_) { /* ignore */ }
      alert(`Не удалось запустить: ${payload.error || res.status}`);
      return;
    }
    let payload = {};
    try { payload = await res.json(); } catch (_) { /* ignore */ }
    pendingJobId = payload.job_id || null;
    analysisState.objectCache.delete(ref);
    applyLocalRunningState(ref, action);
    startPolling(ref);
  }

  function stopAllTimers() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (tickTimer) { clearInterval(tickTimer); tickTimer = null; }
    pollingRef = null;
  }

  function clearPending() {
    pendingJobId = null;
    pendingAction = null;
    pendingStartedAt = null;
  }

  function startPolling(ref) {
    stopAllTimers();
    pollingRef = ref;
    tickTimer = setInterval(updateButtonTickLabel, 1000);
    pollTimer = setInterval(() => pollOnce(ref), 2000);
    pollOnce(ref);
  }

  async function reloadObject(ref) {
    try {
      const data = await getObjectData(ref);
      analysisState.selectedObject = data;
      renderObjectDetail();
    } catch (err) {
      console.error('failed to reload object after summary job', err);
    }
  }

  async function pollOnce(ref) {
    if (pollingRef !== ref) return;
    if (analysisState.selectedRef !== ref) {
      stopAllTimers();
      clearPending();
      return;
    }
    const url = withToken(`${API_PREFIX}/analysis/object-summary/status`, { ref });
    let payload;
    try {
      const res = await fetch(url, { method: 'GET' });
      if (!res.ok) return;
      payload = await res.json();
    } catch (err) {
      return;
    }
    const active = payload && payload.active_job;

    if (!active) {
      // Manager forgot about it — treat as terminal, reconcile UI.
      stopAllTimers();
      clearPending();
      analysisState.objectCache.delete(ref);
      await reloadObject(ref);
      return;
    }

    if (active.qualified_name !== ref) {
      // Another object is being processed. While the foreign job is still
      // running, keep showing the busy button and sync its started_at so
      // the local 1s tick stays accurate. Once the foreign job reaches a
      // terminal status, reload our view so the button unlocks.
      if (active.status === 'running') {
        const busyBtn = document.querySelector('.summary-action-button[data-summary-action="busy"]');
        if (busyBtn && active.started_at) {
          busyBtn.dataset.summaryStartedAt = active.started_at;
        }
        return;
      }
      stopAllTimers();
      clearPending();
      analysisState.objectCache.delete(ref);
      await reloadObject(ref);
      return;
    }

    if (active.status === 'running') {
      const btn = document.querySelector('.summary-action-button');
      if (btn && active.started_at) {
        btn.dataset.summaryStartedAt = active.started_at;
      }
      return;
    }

    // Terminal for our ref. React only if it matches our pending job_id,
    // or if we have no pending tracking (job started elsewhere).
    const isOurs = pendingJobId && active.job_id === pendingJobId;
    if (pendingJobId && !isOurs) return;

    const errMsg = active.status === 'failed'
      ? `Сводка не обновлена: ${active.error || 'ошибка генерации'}`
      : null;
    stopAllTimers();
    clearPending();
    analysisState.objectCache.delete(ref);
    await reloadObject(ref);
    if (errMsg) alert(errMsg);
  }

  function updateButtonTickLabel() {
    if (!pollingRef) return;
    if (analysisState.selectedRef !== pollingRef) return;
    const btn = document.querySelector('.summary-action-button');
    if (!btn) return;
    const startedAt = btn.dataset.summaryStartedAt;
    if (!startedAt) return;
    const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(startedAt)) / 1000));
    const action = btn.dataset.summaryAction;
    let verb = 'Обновление';
    if (action === 'create') verb = 'Создание';
    else if (action === 'busy') verb = 'Занято';
    btn.textContent = `${verb} ${elapsed}с`;
  }

  return { attach, startPolling, stopAllTimers };
})();

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', objectSummaryController.attach);
}

function renderStructureTab(data, sections) {
  const structure = data.structure || {};
  return sections.map((section) => {
    const items = structure[section] || [];
    return `
      <details class="detail-collapse">
        <summary>
          <span>${escapeHtml(STRUCTURE_TITLES[section] || section)}</span>
          <span class="section-count">${items.length}</span>
        </summary>
        ${items.length ? `
          <div class="list-table">
            ${items.map((item) => `
              <button class="row-button compact-row" type="button"
                      data-node-ref="${escapeHtml(item.qualified_name || '')}"
                      title="${escapeHtml(fullTreeTitle(item.name, item.synonym, item.type))}">
                <span>${escapeHtml(item.name || '')}</span>
                <span>${escapeHtml(formatValue(item.type || ''))}</span>
              </button>
            `).join('')}
          </div>
        ` : '<div class="muted">Нет данных</div>'}
      </details>
    `;
  }).join('');
}

function objectSectionItems(data, section, ref) {
  if (section === 'modules') return objectGroupedModules(data);
  const structure = data.structure || {};
  if (section === 'tabular_part_attributes') {
    return (structure.tabular_part_attributes || []).filter((item) => item.parent_qualified_name === ref);
  }
  return structure[section] || [];
}

function objectSectionTitle(data, section, ref) {
  const title = STRUCTURE_TITLES[section] || section;
  if (section !== 'tabular_part_attributes') return title;
  const tabularPart = (data.structure?.tabular_parts || []).find((item) => item.qualified_name === ref);
  return tabularPart?.name ? `${title}: ${tabularPart.name}` : title;
}

function renderSectionItemRow(item, section) {
  if (section === 'modules') {
    return `
      <div class="table-row">
        <span>
          ${escapeHtml(item.module_name || item.module_type || 'Модуль')}
          <small>${escapeHtml(item.module_type || '')}</small>
        </span>
        <span>${Number(item.routine_count || 0).toLocaleString()} процедур</span>
      </div>
    `;
  }
  return `
    <button class="row-button compact-row section-item-row" type="button"
            data-node-ref="${escapeHtml(item.qualified_name || '')}"
            title="${escapeHtml(fullTreeTitle(item.name, item.synonym, item.type))}">
      <span>${escapeHtml(item.name || '')}</span>
      <span>${escapeHtml(formatValue(item.type || item.synonym || ''))}</span>
    </button>
  `;
}

function renderObjectSectionDetail(data, section, ref) {
  setDetailPanelMode('');
  const id = data.identity || {};
  const items = objectSectionItems(data, section, ref);
  const title = objectSectionTitle(data, section, ref);
  $('analysis-detail').innerHTML = `
    <div class="detail-title-row">
      <div>
        <div class="detail-topline">
          ${renderBackButton()}
          <div class="detail-kicker">${escapeHtml(id.name || '')}</div>
        </div>
        <h2>${escapeHtml(title)}</h2>
        <p class="qn">${escapeHtml(id.qualified_name || '')}</p>
      </div>
    </div>
    <div class="metric-row compact">
      ${metricHtml('Элементы', items.length)}
    </div>
    <div class="section-block">
      <h3>${escapeHtml(title)} <span class="section-count">${items.length}</span></h3>
      ${items.length ? `
        <div class="list-table section-item-list">
          ${items.map((item) => renderSectionItemRow(item, section)).join('')}
        </div>
      ` : '<div class="muted">Нет данных</div>'}
    </div>
  `;
  markActiveTreeNode();
}

function getVisibleRelationshipGroups() {
  return (analysisState.relationships?.groups || []).filter((group) => {
    return (group.items || []).length > 0;
  });
}

function renderAccessTab() {
  analysisState.rightConditions = new Map();
  analysisState.rightConditionSeq = 0;
  const groups = getVisibleRelationshipGroups().filter((group) => group.key === 'role_grants');
  if (!groups.length) return '<div class="empty-state">Нет прав</div>';
  return renderRelationshipGroups(groups);
}

function renderRelationshipsTab() {
  analysisState.rightConditions = new Map();
  analysisState.rightConditionSeq = 0;
  const groups = getVisibleRelationshipGroups().filter((group) => group.key !== 'role_grants');
  if (!groups.length) return '<div class="empty-state">Нет связей</div>';
  return renderRelationshipGroups(groups, { collapsed: true });
}

function renderRelationshipGroups(groups, options = {}) {
  if (options.collapsed) {
    return groups.map((group) => `
      <details class="detail-collapse">
        <summary>
          <span>${escapeHtml(relationshipGroupTitle(group))}</span>
          <span class="section-count">${group.items.length}</span>
        </summary>
        ${renderRelationshipGroupBody(group)}
      </details>
    `).join('');
  }

  return groups.map((group) => `
    <div class="section-block">
      <h3>${escapeHtml(relationshipGroupTitle(group))} <span class="section-count">${group.items.length}</span></h3>
      ${renderRelationshipGroupBody(group)}
    </div>
  `).join('');
}

function relationshipGroupTitle(group) {
  if (group.key === 'movements') return 'Движения';
  return group.title || '';
}

function shouldOmitRelationshipCategory(groupKey) {
  return groupKey === 'moved_by'
    || groupKey === 'subscriptions'
    || groupKey === 'uses'
    || groupKey === 'used_by'
    || groupKey === 'access';
}

function shouldGroupRelationshipByCategory(groupKey) {
  return groupKey === 'uses' || groupKey === 'used_by';
}

function renderRelationshipGroupBody(group) {
  if (!group.items.length) return '<div class="muted">Нет данных</div>';
  if (group.key === 'extensions') return renderExtensionRelationshipBody(group.items);
  if (group.key === 'movements') return renderMovementRelationshipBody(group.items);
  if (group.key === 'subscriptions') return renderSubscriptionRelationshipBody(group.items);
  if (shouldGroupRelationshipByCategory(group.key)) return renderCategoryRelationshipBody(group.items);
  return `
    <div class="list-table relationship-list">
      ${group.items.map((item) => relationshipRowHtml(item, {
        omitCategory: shouldOmitRelationshipCategory(group.key),
        wrapInfo: group.key === 'access' || group.key === 'role_grants',
      })).join('')}
    </div>
  `;
}

function renderExtensionRelationshipBody(items) {
  return `
    <div class="extension-list">
      ${items.map((extension) => renderExtensionChangeBlock(extension)).join('')}
    </div>
  `;
}

function renderExtensionChangeBlock(extension) {
  const name = displayConfigName(extension.extension_config || extension.name || '');
  const objectModified = extension.object?.modified || [];
  const objectControlled = extension.object?.controlled || [];
  const sections = normalizeExtensionSections(extension.sections || []);
  const stats = extensionChangeStats(extension);
  return `
    <details class="detail-collapse nested-collapse extension-change-block" open>
      <summary>
        <span class="relationship-title">
          ${treeIconHtml('configuration')}
          <span>${escapeHtml(name)}</span>
        </span>
        <span class="extension-summary-badges">
          ${extensionStatBadge('изменено', stats.modified, 'modified')}
          ${extensionStatBadge('контроль', stats.controlled, 'controlled')}
          ${extensionStatBadge('добавлено', stats.own, 'own')}
          ${extensionStatBadge('заимствовано', stats.adopted, 'adopted')}
        </span>
      </summary>
      <div class="extension-change-body">
        ${objectModified.length || objectControlled.length ? `
          <section class="extension-section extension-filter-target" data-extension-filter-states="${escapeHtml(extensionObjectFilterStates(extension).join(' '))}">
            <h4>Свойства объекта</h4>
            ${objectModified.length ? extensionPropertyLine('Изменено', objectModified, 'modified', extension.object?.modified_values || []) : ''}
            ${objectControlled.length ? extensionPropertyLine('Контролируется', objectControlled, 'controlled') : ''}
          </section>
        ` : ''}
        ${sections.map((section) => renderExtensionSection(section)).join('')}
      </div>
    </details>
  `;
}

function extensionChangeStats(extension) {
  const objectModified = (extension.object?.modified || []).length > 0;
  const objectControlled = (extension.object?.controlled || []).length > 0;
  const objectAdopted = String(extension.ownership || '').toLowerCase().includes('заимств');
  const stats = {
    modified: objectModified ? 1 : 0,
    controlled: objectControlled ? 1 : 0,
    own: 0,
    adopted: objectAdopted ? 1 : 0,
  };
  for (const section of extension.sections || []) {
    for (const item of section.items || []) {
      const modified = isExtensionModifiedItem(item);
      const controlled = (item.controlled || []).length > 0;
      const own = isExtensionOwnItem(item);
      const adopted = isExtensionAdoptedItem(item);
      if (modified) stats.modified += 1;
      if (controlled) stats.controlled += 1;
      if (own) stats.own += 1;
      if (adopted) stats.adopted += 1;
    }
  }
  return stats;
}

function extensionStatBadge(label, value, kind) {
  if (!value) return '';
  const titleByKind = {
    modified: 'Показать/скрыть элементы только с измененными свойствами',
    controlled: 'Показать/скрыть элементы только с контролируемыми свойствами',
    own: 'Показать/скрыть добавленные элементы',
    adopted: 'Показать/скрыть только заимствованные элементы без изменений и контроля',
  };
  return `
    <button class="extension-summary-badge ${kind || ''}"
            type="button"
            data-extension-filter="${escapeHtml(kind || '')}"
            aria-pressed="true"
            title="${escapeHtml(titleByKind[kind] || 'Показать/скрыть элементы')}">
      ${escapeHtml(label)} ${Number(value).toLocaleString()}
    </button>
  `;
}

function extensionPropertyLine(label, values, kind, diffs = []) {
  const diffMap = extensionPropertyDiffMap(diffs);
  return `
    <div class="extension-property-line">
      <span>${escapeHtml(label)}</span>
      <span class="extension-chip-row">
        ${values.map((value) => extensionPropertyChip(value, kind, diffMap.get(value))).join('')}
      </span>
    </div>
  `;
}

function extensionPropertyChip(value, kind, diff) {
  const title = extensionPropertyDiffTitle(value, diff);
  return `<span class="extension-chip ${kind}" title="${escapeHtml(title)}">${escapeHtml(value)}</span>`;
}

function renderExtensionSection(section) {
  if (section.forms) return renderExtensionFormsSection(section);
  if (section.tabularParts) return renderExtensionTabularPartsSection(section);
  const items = section.items || [];
  if (!items.length) return '';
  return `
    <section class="extension-section">
      <h4>${escapeHtml(section.title || 'Элементы')} <span class="section-count">${items.length}</span></h4>
      <div class="extension-item-list">
        ${items.map(renderExtensionItem).join('')}
      </div>
    </section>
  `;
}

function renderExtensionTabularPartsSection(section) {
  const tabularParts = section.tabularParts || [];
  if (!tabularParts.length) return '';
  return `
    <section class="extension-section">
      <h4>${escapeHtml(section.title || 'Табличные части')} <span class="section-count">${tabularParts.length}</span></h4>
      <div class="extension-object-list">
        ${tabularParts.map(renderExtensionTabularPartBlock).join('')}
      </div>
    </section>
  `;
}

function renderExtensionTabularPartBlock(tabularPart) {
  const item = tabularPart.item || { path: tabularPart.name, name: tabularPart.name, label: 'TabularPart' };
  const groups = tabularPart.groups || [];
  return `
    <div class="extension-object-block">
      ${renderExtensionItem(item)}
      ${groups.length ? `
        <div class="extension-object-groups">
          ${groups.map((group) => `
            <div class="extension-object-group">
              <h5>${escapeHtml(group.title)} <span class="section-count">${group.items.length}</span></h5>
              <div class="extension-item-list">
                ${group.items.map(renderExtensionItem).join('')}
              </div>
            </div>
          `).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function renderExtensionFormsSection(section) {
  const forms = section.forms || [];
  if (!forms.length) return '';
  return `
    <section class="extension-section">
      <h4>${escapeHtml(section.title || 'Формы')} <span class="section-count">${forms.length}</span></h4>
      <div class="extension-form-list">
        ${forms.map(renderExtensionFormBlock).join('')}
      </div>
    </section>
  `;
}

function renderExtensionFormBlock(form) {
  const item = form.item || { path: form.name, name: form.name, label: 'Form' };
  const groups = form.groups || [];
  return `
    <div class="extension-form-block">
      ${renderExtensionItem(item)}
      ${groups.length ? `
        <div class="extension-form-groups">
          ${groups.map((group) => `
            <div class="extension-form-group">
              <h5>${escapeHtml(group.title)} <span class="section-count">${group.items.length}</span></h5>
              ${group.title === 'Элементы'
                ? renderExtensionElementTree(group.items)
                : `<div class="extension-item-list">${group.items.map(renderExtensionItem).join('')}</div>`}
            </div>
          `).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function renderExtensionElementTree(items) {
  const nodes = buildExtensionPathTree(items || []);
  if (!nodes.length) return '<div class="muted">Нет данных</div>';
  return `
    <div class="extension-element-tree">
      ${nodes.map((node) => renderExtensionElementNode(node)).join('')}
    </div>
  `;
}

function buildExtensionPathTree(items) {
  const roots = [];
  const byPath = new Map();

  for (const item of items || []) {
    const rawPath = item.path || item.name || '';
    const parts = rawPath.split('/').map((part) => part.trim()).filter(Boolean);
    if (!parts.length) continue;

    let parent = null;
    let fullPath = '';
    for (const part of parts) {
      fullPath = fullPath ? `${fullPath}/${part}` : part;
      if (!byPath.has(fullPath)) {
        const node = {
          path: fullPath,
          name: part,
          item: {
            path: part,
            name: part,
            full_path: fullPath,
            label: 'FormControl',
            is_virtual: true,
          },
          children: [],
        };
        byPath.set(fullPath, node);
        if (parent) parent.children.push(node);
        else roots.push(node);
      }
      parent = byPath.get(fullPath);
    }

    parent.item = {
      ...item,
      path: parts[parts.length - 1],
      full_path: item.full_path || rawPath,
    };
  }

  return roots;
}

function renderExtensionElementNode(node) {
  return `
    <div class="extension-element-node">
      ${renderExtensionItem(node.item)}
      ${node.children.length ? `
        <div class="extension-element-children">
          ${node.children.map((child) => renderExtensionElementNode(child)).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function renderExtensionItem(item) {
  const badges = extensionItemBadges(item);
  const modified = item.modified || [];
  const controlled = item.controlled || [];
  const modifiedDiffs = extensionPropertyDiffMap(item.modified_values || []);
  const filterStates = extensionItemFilterStates(item);
  const props = [
    ...modified.map((value) => ({ kind: 'modified', value, diff: modifiedDiffs.get(value) })),
    ...controlled.map((value) => ({ kind: 'controlled', value })),
  ];
  const title = extensionItemTitle(item, modified, controlled);
  return `
    <div class="extension-item-row extension-filter-target" title="${escapeHtml(title)}" data-extension-filter-states="${escapeHtml(filterStates.join(' '))}">
      <span class="extension-item-main">
        ${extensionItemIconHtml(item)}
        <span>${escapeHtml(item.path || item.name || '')}</span>
      </span>
      <span class="extension-item-meta">
        ${badges.map((badge) => `<span class="extension-chip ${badge.kind}">${escapeHtml(badge.label)}</span>`).join('')}
        ${props.map((prop) => extensionPropertyChip(prop.value, prop.kind, prop.diff)).join('')}
      </span>
    </div>
  `;
}

function extensionItemTitle(item, modified, controlled) {
  const lines = [item.full_path || item.path || item.name || ''];
  if (modified.length) lines.push(`Изменено: ${modified.join(', ')}`);
  if (controlled.length) lines.push(`Контроль: ${controlled.join(', ')}`);
  return lines.filter(Boolean).join('\n');
}

function extensionPropertyDiffMap(diffs) {
  const map = new Map();
  for (const diff of diffs || []) {
    if (diff?.property) map.set(diff.property, diff);
  }
  return map;
}

function extensionPropertyDiffTitle(property, diff) {
  const lines = [property];
  if (!diff || (!diff.has_base && !diff.has_extension)) return lines.join('\n');
  lines.push(`База: ${diff.has_base ? formatExtensionDiffValue(diff.base) : ''}`);
  lines.push(`Расширение: ${diff.has_extension ? formatExtensionDiffValue(diff.extension) : ''}`);
  return lines.join('\n');
}

function formatExtensionDiffValue(value) {
  if (value == null) return '';
  if (Array.isArray(value)) return value.map(formatExtensionDiffValue).join(', ');
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch (_err) {
      return String(value);
    }
  }
  return String(value);
}

function toggleExtensionFilter(button) {
  const isPressed = button.getAttribute('aria-pressed') !== 'false';
  button.setAttribute('aria-pressed', isPressed ? 'false' : 'true');
  button.classList.toggle('is-off', isPressed);
  const block = button.closest('.extension-change-block');
  if (block) applyExtensionFilters(block);
}

function applyExtensionFilters(block) {
  const active = new Set(
    Array.from(block.querySelectorAll('button[data-extension-filter][aria-pressed="true"]'))
      .map((button) => button.dataset.extensionFilter)
      .filter(Boolean)
  );

  block.querySelectorAll('.extension-filter-target').forEach((target) => {
    const states = String(target.dataset.extensionFilterStates || '').split(/\s+/).filter(Boolean);
    target.hidden = !isExtensionTargetVisible(states, active);
  });

  const containerSelectors = [
    '.extension-element-node',
    '.extension-form-group',
    '.extension-object-group',
    '.extension-form-block',
    '.extension-object-block',
    '.extension-section:not(.extension-filter-target)',
  ];
  for (const selector of containerSelectors) {
    Array.from(block.querySelectorAll(selector)).reverse().forEach((container) => {
      container.hidden = !container.querySelector('.extension-filter-target:not([hidden])');
    });
  }
}

function isExtensionTargetVisible(states, active) {
  if (!states.length) return true;

  const hasOwn = states.includes('own');
  const hasAdopted = states.includes('adopted');
  if (hasOwn && !active.has('own')) return false;
  if (hasAdopted && !active.has('adopted')) return false;

  const propertyStates = states.filter((state) => state === 'modified' || state === 'controlled');
  if (propertyStates.length && !propertyStates.some((state) => active.has(state))) return false;

  return true;
}

function extensionObjectFilterStates(extension) {
  const states = [];
  if (String(extension.ownership || '').toLowerCase().includes('заимств')) states.push('adopted');
  if ((extension.object?.modified || []).length) states.push('modified');
  if ((extension.object?.controlled || []).length) states.push('controlled');
  return states;
}

function extensionItemFilterStates(item) {
  const states = [];
  if (isExtensionOwnItem(item)) states.push('own');
  if (isExtensionAdoptedItem(item)) states.push('adopted');
  if (isExtensionModifiedItem(item)) states.push('modified');
  if ((item.controlled || []).length) states.push('controlled');
  return states;
}

function normalizeExtensionSections(sections) {
  const forms = new Map();
  const tabularParts = new Map();
  const result = [];
  const formChildSections = {
    'Реквизиты форм': { marker: '.Реквизиты.', title: 'Реквизиты' },
    'Элементы форм': { marker: '.Элементы.', title: 'Элементы' },
    'Команды форм': { marker: '.Команды.', title: 'Команды' },
  };
  const tabularPartChildSections = {
    'Реквизиты табличных частей': { marker: '.Реквизиты.', title: 'Реквизиты' },
  };

  const ensureForm = (name, item = null) => {
    const key = name || item?.name || item?.path || 'Форма';
    if (!forms.has(key)) {
      forms.set(key, {
        name: key,
        item: item || { path: key, name: key, label: 'Form' },
        groups: [],
        groupMap: new Map(),
      });
    }
    const form = forms.get(key);
    if (item) form.item = item;
    return form;
  };

  const ensureTabularPart = (name, item = null) => {
    const key = name || item?.name || item?.path || 'Табличная часть';
    if (!tabularParts.has(key)) {
      tabularParts.set(key, {
        name: key,
        item: item || { path: key, name: key, label: 'TabularPart' },
        groups: [],
        groupMap: new Map(),
      });
    }
    const tabularPart = tabularParts.get(key);
    if (item) tabularPart.item = item;
    return tabularPart;
  };

  for (const section of sections || []) {
    if (section.title === 'Табличные части') {
      for (const item of section.items || []) {
        ensureTabularPart(item.path || item.name, item);
      }
      continue;
    }

    const tabularChildSpec = tabularPartChildSections[section.title];
    if (tabularChildSpec) {
      for (const item of section.items || []) {
        const parsed = splitExtensionFormPath(item.path || item.name || '', tabularChildSpec.marker);
        const tabularPart = ensureTabularPart(parsed.formName);
        if (!tabularPart.groupMap.has(tabularChildSpec.title)) {
          const group = { title: tabularChildSpec.title, items: [] };
          tabularPart.groupMap.set(tabularChildSpec.title, group);
          tabularPart.groups.push(group);
        }
        tabularPart.groupMap.get(tabularChildSpec.title).items.push({
          ...item,
          full_path: item.path || item.name || '',
          path: parsed.childPath,
        });
      }
      continue;
    }

    if (section.title === 'Формы') {
      for (const item of section.items || []) {
        ensureForm(item.path || item.name, item);
      }
      continue;
    }

    const childSpec = formChildSections[section.title];
    if (childSpec) {
      for (const item of section.items || []) {
        const parsed = splitExtensionFormPath(item.path || item.name || '', childSpec.marker);
        const form = ensureForm(parsed.formName);
        if (!form.groupMap.has(childSpec.title)) {
          const group = { title: childSpec.title, items: [] };
          form.groupMap.set(childSpec.title, group);
          form.groups.push(group);
        }
        form.groupMap.get(childSpec.title).items.push({
          ...item,
          full_path: item.path || item.name || '',
          path: parsed.childPath,
        });
      }
      continue;
    }

    result.push(section);
  }

  if (tabularParts.size) {
    for (const tabularPart of tabularParts.values()) {
      delete tabularPart.groupMap;
    }
    result.push({
      title: 'Табличные части',
      tabularParts: Array.from(tabularParts.values()),
    });
  }

  if (forms.size) {
    for (const form of forms.values()) {
      delete form.groupMap;
    }
    result.push({
      title: 'Формы',
      forms: Array.from(forms.values()),
    });
  }

  return result;
}

function splitExtensionFormPath(path, marker) {
  const index = path.indexOf(marker);
  if (index >= 0) {
    return {
      formName: path.slice(0, index),
      childPath: path.slice(index + marker.length) || path,
    };
  }
  const dotIndex = path.indexOf('.');
  if (dotIndex >= 0) {
    return {
      formName: path.slice(0, dotIndex),
      childPath: path.slice(dotIndex + 1) || path,
    };
  }
  return {
    formName: 'Форма',
    childPath: path,
  };
}

function extensionItemIconHtml(item) {
  const iconByLabel = {
    Attribute: 'attribute',
    TabularPart: 'tabular_section',
    Resource: 'register_resource',
    Dimension: 'register_dimension',
    Form: 'form',
    Command: 'command',
    Layout: 'template',
    JournalGraph: 'journal_graph',
    Module: 'module',
    FormControl: 'form_control_field',
    FormAttribute: 'attribute',
  };
  return treeIconHtml(iconByLabel[item.label] || 'common');
}

function extensionItemBadges(item) {
  const badges = [];
  if (isExtensionOwnItem(item)) badges.push({ kind: 'own', label: 'добавлено' });
  const adopted = isExtensionAdoptedItem(item);
  const hasPropertyState = isExtensionModifiedItem(item) || (item.controlled || []).length > 0;
  if (adopted && !badges.length && !hasPropertyState) badges.push({ kind: 'adopted', label: 'заимствовано' });
  return badges;
}

function isExtensionModifiedItem(item) {
  return (item.modified || []).length > 0;
}

function isExtensionAdoptedItem(item) {
  const ownership = String(item.ownership || '').toLowerCase();
  return item.is_adopted || item.ext_source === 'adopted_modified' || ownership.includes('заимств');
}

function isExtensionOwnItem(item) {
  const ownership = String(item.ownership || '').toLowerCase();
  return item.ext_source === 'own' || ownership.includes('собствен');
}

function renderMovementRelationshipBody(items) {
  return renderCategoryRelationshipBody(items);
}

function renderSubscriptionRelationshipBody(items) {
  const byEvent = new Map();
  for (const item of items || []) {
    const key = formatValue(item.via || 'Без события');
    if (!byEvent.has(key)) byEvent.set(key, []);
    byEvent.get(key).push(item);
  }
  return [...byEvent.entries()]
    .sort(([left], [right]) => left.localeCompare(right, 'ru'))
    .map(([eventName, eventItems]) => `
      <details class="detail-collapse nested-collapse subscription-event-group" open>
        <summary>
          <span class="relationship-title">
            <span>${escapeHtml(eventName)}</span>
          </span>
          <span class="section-count">${eventItems.length}</span>
        </summary>
        <div class="list-table relationship-list">
          ${eventItems
            .sort((left, right) => String(left.name || '').localeCompare(String(right.name || ''), 'ru'))
            .map((item) => relationshipRowHtml(item, { omitCategory: true, hideInfo: true }))
            .join('')}
        </div>
      </details>
    `).join('');
}

function renderCategoryRelationshipBody(items) {
  const byCategory = new Map();
  for (const item of items || []) {
    const key = item.category || '';
    if (!byCategory.has(key)) byCategory.set(key, []);
    byCategory.get(key).push(item);
  }
  return [...byCategory.entries()].map(([category, categoryItems]) => `
    <details class="detail-collapse nested-collapse" open>
      <summary>
        <span class="relationship-title">
          ${treeIconHtml(categoryIconName(category))}
          <span>${escapeHtml(categoryDisplayName(category))}</span>
        </span>
        <span class="section-count">${categoryItems.length}</span>
      </summary>
      <div class="list-table relationship-list">
        ${categoryItems.map((item) => relationshipRowHtml(item, { omitCategory: true })).join('')}
      </div>
    </details>
  `).join('');
}

function relationshipRowHtml(item, options = {}) {
  const category = item.category || '';
  const label = options.omitCategory ? (item.name || '') : `${category}.${item.name || ''}`;
  const rightsHtml = relationshipRightsHtml(item);
  const relationInfo = options.hideInfo ? '' : relationshipInfoText(item);
  const rowClass = [
    'row-button',
    relationInfo || rightsHtml ? 'relationship-row' : 'single-column-row',
    options.wrapInfo ? 'relationship-row-wrap-info' : '',
  ].filter(Boolean).join(' ');
  const tag = rightsHtml ? 'div' : 'button';
  const typeAttr = rightsHtml ? '' : ' type="button"';
  return `
    <${tag} class="${rowClass}"${typeAttr} data-ref="${escapeHtml(item.qualified_name || '')}"${rightsHtml ? ' role="button" tabindex="0"' : ''}>
      <span class="relationship-title">
        ${treeIconHtml(categoryIconName(category))}
        <span>${escapeHtml(label)}</span>
      </span>
      ${rightsHtml || (relationInfo ? `<span class="relationship-via" title="${escapeHtml(relationInfo)}">${escapeHtml(relationInfo)}</span>` : '')}
    </${tag}>
  `;
}

function relationshipInfoText(item) {
  return formatValue(item.via || '');
}

function relationshipRightsHtml(item) {
  if (!Array.isArray(item.rights) || !item.rights.length) return '';
  const rights = item.rights
    .map((right) => ({
      name: String(right?.name || '').trim(),
      condition: String(right?.condition || '').trim(),
      hasCondition: Boolean(right?.has_condition || right?.condition),
    }))
    .filter((right) => right.name);
  if (!rights.length) return '';
  return `
    <span class="relationship-via relationship-rights">
      ${rights.map((right) => relationshipRightChipHtml(right, item)).join('')}
    </span>
  `;
}

function relationshipRightChipHtml(right, item) {
  if (!right.hasCondition) {
    return `<span class="right-chip">${escapeHtml(right.name)}</span>`;
  }
  const id = registerRightCondition({
    title: right.name,
    condition: right.condition,
    owner: item.name || '',
    category: item.category || '',
  });
  return `
    <button class="right-chip right-chip-condition" type="button"
            data-right-condition="${escapeHtml(id)}"
            title="Открыть условие ограничения">
      <span>${escapeHtml(right.name)}</span>
      <span class="right-condition-mark">условие</span>
    </button>
  `;
}

function registerRightCondition(payload) {
  const id = `right-condition-${++analysisState.rightConditionSeq}`;
  analysisState.rightConditions.set(id, payload);
  return id;
}

function showRightConditionModal(id) {
  const payload = analysisState.rightConditions.get(id);
  if (!payload) return;
  const modal = ensureRightConditionModal();
  modal.querySelector('[data-role="right-title"]').textContent = payload.title || 'Право';
  modal.querySelector('[data-role="right-owner"]').textContent = payload.owner || '';
  modal.querySelector('[data-role="right-condition-text"]').textContent = payload.condition || 'Условие не найдено';
  modal.hidden = false;
  modal.querySelector('[data-role="right-close"]').focus();
}

function ensureRightConditionModal() {
  let modal = document.getElementById('right-condition-modal');
  if (modal) return modal;
  modal = document.createElement('div');
  modal.id = 'right-condition-modal';
  modal.className = 'condition-modal';
  modal.hidden = true;
  modal.innerHTML = `
    <div class="condition-modal-backdrop" data-role="right-close"></div>
    <section class="condition-modal-dialog" role="dialog" aria-modal="true" aria-labelledby="right-condition-title">
      <header class="condition-modal-header">
        <div>
          <h3 id="right-condition-title" data-role="right-title"></h3>
          <p data-role="right-owner"></p>
        </div>
        <button class="panel-icon-button" type="button" data-role="right-close" title="Закрыть">×</button>
      </header>
      <pre data-role="right-condition-text" class="condition-modal-text"></pre>
    </section>
  `;
  modal.addEventListener('click', (event) => {
    if (event.target.closest('[data-role="right-close"]')) {
      modal.hidden = true;
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.hidden) modal.hidden = true;
  });
  document.body.appendChild(modal);
  return modal;
}

function renderModulesTab(data) {
  const modules = objectGroupedModules(data);
  return `
    <div class="section-block">
      <h3>Модули <span class="section-count">${modules.length}</span></h3>
      ${modules.length ? `
        <div class="list-table">
          ${modules.map((module) => `
            <div class="table-row">
              <span>
                ${escapeHtml(module.module_name || module.module_type || 'Модуль')}
                <small>${escapeHtml(module.module_type || '')}</small>
              </span>
              <span>${Number(module.routine_count || 0).toLocaleString()} процедур</span>
            </div>
          `).join('')}
        </div>
      ` : '<div class="muted">Нет данных</div>'}
    </div>
  `;
}

function renderProperties(groups) {
  const visibleGroups = (groups || [])
    .map((group) => ({
      ...group,
      items: (group.items || []).filter((item) => !isTechnicalPropertyKey(item.key)),
    }))
    .filter((group) => group.items.length > 0);
  if (!visibleGroups.length) return '<div class="empty-state">Нет свойств</div>';
  return visibleGroups.map((group) => `
    <div class="section-block">
      <h3>${escapeHtml(group.title)}</h3>
      <dl class="property-list">
        ${(group.items || []).map((item) => definitionHtml(item.key, item.value)).join('')}
      </dl>
    </div>
  `).join('');
}

function renderNodeEvents(events) {
  const list = (events || []).filter((event) => event && (event.name || event.qualified_name));
  if (!list.length) return '';

  const rows = [];
  let showActionColumn = false;
  list.forEach((event) => {
    const actions = Array.isArray(event.actions) && event.actions.length ? event.actions : [null];
    actions.forEach((action, index) => {
      if (nodeEventNeedsActionColumn(action)) showActionColumn = true;
      rows.push({ event, action, first: index === 0 });
    });
  });

  return `
    <div class="section-block node-events-block">
      <h3>События</h3>
      <div class="node-events-table${showActionColumn ? ' has-action-column' : ''}">
        <div class="node-events-cell node-events-head">Событие</div>
        ${showActionColumn ? '<div class="node-events-cell node-events-head">Действие</div>' : ''}
        <div class="node-events-cell node-events-head">Обработчик</div>
        ${rows.map(({ event, action }) => `
          <div class="node-events-cell node-events-name">${escapeHtml(event.name || '')}</div>
          ${showActionColumn ? `<div class="node-events-cell">${escapeHtml(formatNodeEventAction(action))}</div>` : ''}
          <div class="node-events-cell node-events-handler">${escapeHtml(formatNodeEventHandler(action))}</div>
        `).join('')}
      </div>
    </div>
  `;
}

function nodeEventNeedsActionColumn(action) {
  if (!action) return false;
  const callType = String(action.call_type || '').toLowerCase();
  return Boolean(
    action.extends_base_action
    || Number(action.extension_action_count || 0) > 0
    || (callType && callType !== 'main' && callType !== 'основное')
  );
}

function formatNodeEventAction(action) {
  if (!action) return '—';
  const labels = {
    Main: 'Основное',
    Before: 'Перед',
    After: 'После',
    Override: 'Вместо',
  };
  const value = String(action.call_type || '').trim();
  return labels[value] || value || '—';
}

function formatNodeEventHandler(action) {
  if (!action) return '—';
  return String(action.handler_name || action.routine_name || '').trim() || '—';
}

function isTechnicalPropertyKey(key) {
  return String(key || '').startsWith('console_');
}

function definitionHtml(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(formatValue(value))}</dd>`;
}

function metricHtml(label, value) {
  return `
    <div class="metric">
      <span>${escapeHtml(label)}</span>
      <strong>${Number(value || 0).toLocaleString()}</strong>
    </div>
  `;
}

function counterLabel(key) {
  const labels = {
    attributes: 'Реквизиты',
    standard_attributes: 'Станд. рекв.',
    tabular_parts: 'ТЧ',
    resources: 'Ресурсы',
    dimensions: 'Измерения',
    forms: 'Формы',
    commands: 'Команды',
    layouts: 'Макеты',
    journal_graphs: 'Графы',
    enum_values: 'Значения',
    predefined: 'Предопр.',
    modules: 'Модули',
    movements: 'Движения',
  };
  return labels[key] || key;
}

function moduleTypeLabel(type) {
  const labels = {
    CommonModule: 'Общий модуль',
    ObjectModule: 'Модуль объекта',
    ManagerModule: 'Модуль менеджера',
    FormModule: 'Модуль формы',
    CommonFormModule: 'Модуль формы',
    CommandModule: 'Модуль команды',
    ConfigurationModule: 'Модуль конфигурации',
    ExternalConnectionModule: 'Модуль внешнего соединения',
    ManagedApplicationModule: 'Модуль управляемого приложения',
    OrdinaryApplicationModule: 'Модуль обычного приложения',
    SessionModule: 'Модуль сеанса',
    ValueManagerModule: 'Модуль менеджера значения',
    RecordSetModule: 'Модуль набора записей',
  };
  return labels[type] || type || '';
}

function nodeLabel(label) {
  const labels = {
    Configuration: 'Конфигурация',
    MetadataCategory: 'Категория',
    MetadataObject: 'Объект метаданных',
    Attribute: 'Реквизит',
    TabularPart: 'Табличная часть',
    Resource: 'Ресурс',
    Dimension: 'Измерение',
    Form: 'Форма',
    FormControl: 'Элемент формы',
    FormAttribute: 'Реквизит формы',
    Command: 'Команда',
    Layout: 'Макет',
    JournalGraph: 'Граф журнала документов',
    EnumValue: 'Значение перечисления',
    PredefinedItem: 'Предопределенный элемент',
    Module: 'Модуль',
    Routine: 'Процедура',
  };
  return labels[label] || label || 'Узел';
}

function renderBackButton() {
  if (!analysisState.backStack.length) return '';
  return '<button class="back-button" type="button" data-action="back" title="Назад" aria-label="Назад">←</button>';
}

function renderBadgesInline(badges = []) {
  if (!badges.length) return '';
  return `
    <div class="inline-badges">
      ${badges.map((badge) => `
        <span class="${escapeHtml(badge.kind || '')}"
              title="${escapeHtml(badge.title || badge.label || '')}">${escapeHtml(badge.label)}</span>
      `).join('')}
    </div>
  `;
}

function markActiveTreeNode() {
  document.querySelectorAll('.tree-node.is-active').forEach((node) => {
    node.classList.remove('is-active');
  });

  const nodes = [...document.querySelectorAll('.tree-node')];
  let active = nodes.find((node) => {
    if (analysisState.selectedKind === 'project') return node.dataset.kind === 'project';
    if (analysisState.selectedKind === 'config') {
      return node.dataset.kind === 'config' && node.dataset.config === analysisState.selectedConfig;
    }
    if (analysisState.selectedKind === 'category') {
      return node.dataset.kind === 'category'
        && node.dataset.config === analysisState.selectedConfig
        && node.dataset.category === analysisState.selectedCategory;
    }
    if (analysisState.selectedKind === 'category-group') {
      return node.dataset.kind === 'category-group'
        && node.dataset.config === analysisState.selectedConfig
        && node.dataset.group === analysisState.selectedCategory;
    }
    if (analysisState.selectedKind === 'object') {
      return node.dataset.kind === 'object' && node.dataset.ref === analysisState.selectedRef;
    }
    if (analysisState.selectedKind === 'object-section') {
      return node.dataset.kind === 'object-section'
        && node.dataset.ref === analysisState.selectedRef
        && node.dataset.section === analysisState.selectedSection;
    }
    if (analysisState.selectedKind === 'node') {
      return node.dataset.kind === 'node' && node.dataset.ref === analysisState.selectedRef;
    }
    if (analysisState.selectedKind === 'module') {
      return node.dataset.kind === 'module'
        && moduleSelectionKey(node.dataset.moduleId, node.dataset.ownerRef, node.dataset.moduleType)
          === moduleSelectionKey(
            analysisState.selectedModuleId,
            analysisState.selectedModuleOwner,
            analysisState.selectedModuleType,
          );
    }
    return false;
  });
  if (!active && analysisState.selectedKind === 'module'
      && analysisState.selectedModuleType === 'CommonModule'
      && analysisState.selectedModuleOwner) {
    active = nodes.find((node) => {
      return node.dataset.kind === 'object' && node.dataset.ref === analysisState.selectedModuleOwner;
    });
  }
  if (!active && analysisState.selectedKind === 'node') {
    active = nodes.find((node) => {
      return node.dataset.kind === 'object' && node.dataset.ref === parentObjectRef(analysisState.selectedRef);
    });
  }
  if (active) active.classList.add('is-active');
  return active || null;
}

function revealActiveTreeNode() {
  const active = markActiveTreeNode();
  if (!active) return;
  openAncestorBranches(active);
  active.scrollIntoView({ block: 'center', inline: 'nearest' });
}

function filterTree(text) {
  const needle = String(text || '').trim().toLowerCase();
  document.querySelectorAll('.tree-object-branch').forEach((branch) => {
    const objectText = branch.querySelector(':scope > .tree-row .tree-object')?.textContent.toLowerCase() || '';
    const childMatch = [...branch.querySelectorAll('.tree-object-section-node, .tree-structure-node')].some((node) => {
      return node.textContent.toLowerCase().includes(needle);
    });
    branch.hidden = Boolean(needle) && !objectText.includes(needle) && !childMatch;
  });
  document.querySelectorAll('.tree-category').forEach((node) => {
    const categoryText = node.querySelector('.tree-node')?.textContent.toLowerCase() || '';
    const childMatch = [...node.querySelectorAll('.tree-object-branch')].some((child) => !child.hidden);
    node.hidden = Boolean(needle) && !categoryText.includes(needle) && !childMatch;
  });
  document.querySelectorAll('.tree-category-group').forEach((node) => {
    const groupText = node.querySelector('.tree-node')?.textContent.toLowerCase() || '';
    const childMatch = [...node.querySelectorAll('.tree-category')].some((child) => !child.hidden);
    node.hidden = Boolean(needle) && !groupText.includes(needle) && !childMatch;
  });
}

window.addEventListener('pagehide', saveAnalysisPageState);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') saveAnalysisPageState();
});

const activePage = getActivePage();
setupMermaidFullscreenModal();
showPage(activePage);
if (activePage === 'system') refresh();
if (activePage === 'analysis') initAnalysis();

const statsRefreshBtn = $('stats-refresh-btn');
if (statsRefreshBtn) statsRefreshBtn.addEventListener('click', () => refreshStatsManually());
