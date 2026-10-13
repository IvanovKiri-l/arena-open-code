// Локализация интерфейса: RU/EN. Выбор хранится в localStorage,
// по умолчанию — по языку браузера.
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Lang = "ru" | "en";

const ru = {
  appTitle: "Арена",
  subtitle: "Один промпт — {n} параллельных инстансов модели — один победитель.",
  import: "Импорт",
  export: "Экспорт",
  switchToEn: "Switch to English",
  switchToRu: "Переключить на русский",

  promptPlaceholder: "Введите промпт…",
  verification: "Верификация",
  verificationHintOn: "Прогнать ответы через VERIFY_CMD из .env",
  verificationHintOff: "Задайте VERIFY_CMD в .env, чтобы включить верификацию",
  judgesLabel: "Судей:",
  judgesOne: "1",
  judgesPanel: "3 (жюри)",
  start: "Запустить арену",
  running: "Арена идёт…",
  verifying: "Проверяю код…",
  judging: "Судят…",

  tileWaiting: "ожидание",
  tileStreaming: "стрим…",
  statusError: "ошибка",
  statusDisqualified: "дисквалифицирован",
  latencyShort: "{n} мс",
  tokensShort: "{n} ток.",

  verdict: "Вердикт",
  revote: "Переголосовать",
  revoteBusy: "Судят…",
  winnerLine: "Победитель: слот #{slot} [{profile}]",
  noWinner: "Победитель не определён",
  colSlot: "слот",
  colRole: "роль",
  colScore: "сумма баллов",
  judgesCountShort: "{n} суд.",
  judgeNote: "Судья {n}:",
  showLosers: "Показать проигравших",
  hideLosers: "Свернуть проигравших",

  history: "История",
  answersCount: "ответов: {n}",

  backendOffline: "Бэкенд недоступен — запустите: docker compose up",
  backendOnline: "Бэкенд на связи",
  modelLabel: "модель: {model}",
  modelNotSet: "не задана",
  fillKey: "заполните LLM_API_KEY в .env",
  checkingBackend: "Проверяю бэкенд…",

  importedRun: "Импортированный забег",
  historyRun: "Забег из истории",
  streamTerminated: "поток событий оборван",
} as const;

export type MsgKey = keyof typeof ru;

const en: Record<MsgKey, string> = {
  appTitle: "Arena",
  subtitle: "One prompt — {n} parallel model instances — one winner.",
  import: "Import",
  export: "Export",
  switchToEn: "Switch to English",
  switchToRu: "Переключить на русский",

  promptPlaceholder: "Enter your prompt…",
  verification: "Verification",
  verificationHintOn: "Run answers through VERIFY_CMD from .env",
  verificationHintOff: "Set VERIFY_CMD in .env to enable verification",
  judgesLabel: "Judges:",
  judgesOne: "1",
  judgesPanel: "3 (panel)",
  start: "Start arena",
  running: "Arena running…",
  verifying: "Verifying…",
  judging: "Judging…",

  tileWaiting: "waiting",
  tileStreaming: "streaming…",
  statusError: "error",
  statusDisqualified: "disqualified",
  latencyShort: "{n} ms",
  tokensShort: "{n} tk.",

  verdict: "Verdict",
  revote: "Re-vote",
  revoteBusy: "Judging…",
  winnerLine: "Winner: slot #{slot} [{profile}]",
  noWinner: "No winner determined",
  colSlot: "slot",
  colRole: "role",
  colScore: "total score",
  judgesCountShort: "{n} judges",
  judgeNote: "Judge {n}:",
  showLosers: "Show losers",
  hideLosers: "Hide losers",

  history: "History",
  answersCount: "answers: {n}",

  backendOffline: "Backend unreachable — run: docker compose up",
  backendOnline: "Backend online",
  modelLabel: "model: {model}",
  modelNotSet: "not set",
  fillKey: "set LLM_API_KEY in .env",
  checkingBackend: "Checking backend…",

  importedRun: "Imported run",
  historyRun: "Run from history",
  streamTerminated: "event stream interrupted",
};

const dicts: Record<Lang, Record<MsgKey, string>> = { ru, en };

export function translate(
  lang: Lang,
  key: MsgKey,
  vars?: Record<string, string | number>,
): string {
  let text: string = dicts[lang][key];
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replace(`{${name}}`, String(value));
    }
  }
  return text;
}

type I18nValue = {
  lang: Lang;
  t: (key: MsgKey, vars?: Record<string, string | number>) => string;
  setLang: (lang: Lang) => void;
};

const I18nContext = createContext<I18nValue | null>(null);

function detectLang(): Lang {
  try {
    const saved = localStorage.getItem("arena.lang");
    if (saved === "ru" || saved === "en") {
      return saved;
    }
  } catch {
    // localStorage может быть недоступен — просто не запоминаем
  }
  return typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("ru")
    ? "ru"
    : "en";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectLang);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem("arena.lang", next);
    } catch {
      // не критично, если не удалось сохранить
    }
  }, []);

  const value = useMemo<I18nValue>(
    () => ({
      lang,
      setLang,
      t: (key, vars) => translate(lang, key, vars),
    }),
    [lang, setLang],
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const context = useContext(I18nContext);
  if (!context) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return context;
}
