import { useCallback, useEffect, useRef, useState } from "react";
import ArenaGrid, { type Tile } from "./ArenaGrid";
import VerdictPanel from "./VerdictPanel";
import {
  STREAM_TERMINATED,
  exportRunUrl,
  getHealth,
  getRun,
  listRuns,
  openRunStream,
  rejudge,
  startRun,
} from "./api";
import { useI18n } from "./i18n";
import type {
  Health,
  JudgeResultPayload,
  ResponseRecord,
  RunPayload,
  RunSummary,
  StreamEvent,
  VerdictRecord,
} from "./types";

type Phase = "idle" | "running" | "verifying" | "judging" | "done" | "error";
type Source = "live" | "history" | "import";

type View = {
  runId: string;
  prompt: string;
  tiles: Tile[];
  verdicts: VerdictRecord[];
  winnerId: string | null;
  judgeErrors: string[];
  source: Source;
};

function viewFromPayload(payload: RunPayload, source: Source): View {
  const tiles = [...payload.responses]
    .sort((a, b) => a.slot - b.slot)
    .map((record) => ({ slot: record.slot, text: record.output ?? "", record }));
  return {
    runId: payload.id,
    prompt: payload.prompt,
    tiles,
    verdicts: payload.verdicts ?? [],
    winnerId: payload.winner_id ?? null,
    judgeErrors: [],
    source,
  };
}

function placeholderTiles(count: number): Tile[] {
  return Array.from({ length: Math.max(1, count) }, (_, index) => ({
    slot: index + 1,
    text: "",
    record: null,
  }));
}

function withUpdatedTile(tiles: Tile[], slot: number, update: (tile: Tile) => Tile): Tile[] {
  const next = tiles.map((tile) => (tile.slot === slot ? update(tile) : tile));
  if (!next.some((tile) => tile.slot === slot)) {
    next.push(update({ slot, text: "", record: null }));
  }
  return next.sort((a, b) => a.slot - b.slot);
}

function applyEvent(view: View, event: StreamEvent): View {
  switch (event.type) {
    case "chunk":
      return {
        ...view,
        tiles: withUpdatedTile(view.tiles, event.slot, (tile) => ({
          ...tile,
          text: tile.text + event.text,
        })),
      };
    case "response": {
      const record = event.data;
      return {
        ...view,
        tiles: withUpdatedTile(view.tiles, record.slot, () => ({
          slot: record.slot,
          text: record.output ?? "",
          record,
        })),
      };
    }
    case "verdict":
      return {
        ...view,
        verdicts: event.data.verdicts ?? [],
        winnerId: event.data.winner_id ?? null,
        judgeErrors: event.data.errors ?? [],
      };
    default:
      return view;
  }
}

export default function App() {
  const { lang, setLang, t } = useI18n();
  const [health, setHealth] = useState<Health | null>(null);
  const [offline, setOffline] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [judgesCount, setJudgesCount] = useState(1);
  const [verifyEnabled, setVerifyEnabled] = useState(true);
  const [phase, setPhase] = useState<Phase>("idle");
  const [view, setView] = useState<View | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [revoting, setRevoting] = useState(false);
  const [revealLosers, setRevealLosers] = useState(false);
  const [history, setHistory] = useState<RunSummary[]>([]);
  const closeStream = useRef<(() => void) | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const refreshHistory = useCallback(() => {
    listRuns(12)
      .then(setHistory)
      .catch(() => setOffline(true));
  }, []);

  useEffect(() => {
    getHealth()
      .then((data) => {
        setHealth(data);
        setOffline(false);
      })
      .catch(() => setOffline(true));
    refreshHistory();
    return () => closeStream.current?.();
  }, [refreshHistory]);

  const handleEvent = useCallback(
    (event: StreamEvent) => {
      if (event.type === "verifying") {
        setPhase("verifying");
      }
      if (event.type === "judging") {
        setPhase("judging");
      }
      if (event.type === "verdict") {
        setPhase("done");
        refreshHistory();
      }
      if (event.type === "error") {
        setErrorMessage(
          event.message === STREAM_TERMINATED ? t("streamTerminated") : event.message,
        );
        setPhase("error");
      }
      if (event.type === "end") {
        setPhase((current) =>
          current === "running" || current === "verifying" || current === "judging"
            ? "done"
            : current,
        );
      }
      setView((current) =>
        current && current.source === "live" ? applyEvent(current, event) : current,
      );
    },
    [t, refreshHistory],
  );

  // Свежий обработчик для уже открытого SSE-потока (язык мог переключиться).
  const handleEventRef = useRef(handleEvent);
  useEffect(() => {
    handleEventRef.current = handleEvent;
  }, [handleEvent]);

  const handleStart = useCallback(async () => {
    const text = prompt.trim();
    if (!text || phase === "running" || phase === "verifying" || phase === "judging") {
      return;
    }
    closeStream.current?.();
    setErrorMessage(null);
    setRevealLosers(false);
    setPhase("running");
    setView({
      runId: "",
      prompt: text,
      tiles: placeholderTiles(health?.arena_n ?? 10),
      verdicts: [],
      winnerId: null,
      judgeErrors: [],
      source: "live",
    });

    try {
      const { run_id } = await startRun(text, {
        judges: judgesCount,
        verify: health?.verify_cmd_set ? verifyEnabled : undefined,
      });
      setView((current) =>
        current && current.source === "live" ? { ...current, runId: run_id } : current,
      );
      closeStream.current = openRunStream(run_id, (event) => handleEventRef.current(event));
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
      setPhase("error");
    }
  }, [prompt, phase, health, judgesCount, verifyEnabled]);

  const handleRevote = useCallback(async () => {
    if (!view || view.source === "import" || !view.runId) {
      return;
    }
    setRevoting(true);
    try {
      const result: JudgeResultPayload = await rejudge(view.runId, { judges: judgesCount });
      setView((current) =>
        current
          ? {
              ...current,
              verdicts: result.verdicts ?? [],
              winnerId: result.winner_id ?? null,
              judgeErrors: result.errors ?? [],
            }
          : current,
      );
      refreshHistory();
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setRevoting(false);
    }
  }, [view, judgesCount, refreshHistory]);

  const handleOpenHistory = useCallback(async (runId: string) => {
    closeStream.current?.();
    try {
      const payload = await getRun(runId);
      setView(viewFromPayload(payload, "history"));
      setPrompt(payload.prompt);
      setErrorMessage(null);
      setPhase("done");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
      setPhase("error");
    }
  }, []);

  const handleImportFile = useCallback(async (file: File) => {
    closeStream.current?.();
    try {
      const payload = JSON.parse(await file.text()) as RunPayload;
      if (!payload || typeof payload.id !== "string" || !Array.isArray(payload.responses)) {
        throw new Error("это не похоже на экспорт забега Arena / this is not an Arena run export");
      }
      setView(viewFromPayload(payload, "import"));
      setPrompt(payload.prompt ?? "");
      setErrorMessage(null);
      setPhase("done");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : String(error));
      setPhase("error");
    }
  }, []);

  const winner: ResponseRecord | null =
    view?.winnerId !== null && view?.winnerId !== undefined
      ? (view.tiles.find((tile) => tile.record?.id === view.winnerId)?.record ?? null)
      : null;
  const responses = view
    ? view.tiles
        .map((tile) => tile.record)
        .filter((record): record is ResponseRecord => record !== null)
    : [];
  const busy = phase === "running" || phase === "verifying" || phase === "judging";
  const live = view?.source === "live";

  const startLabel = busy
    ? phase === "verifying"
      ? t("verifying")
      : phase === "judging"
        ? t("judging")
        : t("running")
    : t("start");

  const statusLine = offline
    ? t("backendOffline")
    : health
      ? `${t("backendOnline")} · ${t("modelLabel", {
          model: health.model ?? t("modelNotSet"),
        })}` + (health.llm_configured ? "" : ` · ${t("fillKey")}`)
      : t("checkingBackend");

  return (
    <main className="page">
      <header className="head">
        <div>
          <h1>{t("appTitle")}</h1>
          <p>{t("subtitle", { n: health?.arena_n ?? 10 })}</p>
        </div>
        <div className="head-actions">
          <button
            className="ghost lang-switch"
            onClick={() => setLang(lang === "ru" ? "en" : "ru")}
            title={lang === "ru" ? t("switchToEn") : t("switchToRu")}
          >
            {lang === "ru" ? "EN" : "RU"}
          </button>
          <button
            className="ghost"
            onClick={() => fileInput.current?.click()}
            disabled={busy}
          >
            {t("import")}
          </button>
          {view && view.source !== "import" && view.runId && (
            <a className="ghost" href={exportRunUrl(view.runId)}>
              {t("export")}
            </a>
          )}
          <input
            ref={fileInput}
            type="file"
            accept="application/json,.json"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) {
                void handleImportFile(file);
              }
              event.target.value = "";
            }}
          />
        </div>
      </header>

      <section className="panel">
        <textarea
          placeholder={t("promptPlaceholder")}
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          disabled={busy}
        />
        <div className="panel-side">
          <label
            className="judges-select"
            title={
              health?.verify_cmd_set ? t("verificationHintOn") : t("verificationHintOff")
            }
          >
            {t("verification")}
            <input
              type="checkbox"
              checked={verifyEnabled && Boolean(health?.verify_cmd_set)}
              disabled={busy || !health?.verify_cmd_set}
              onChange={(event) => setVerifyEnabled(event.target.checked)}
            />
          </label>
          <label className="judges-select">
            {t("judgesLabel")}
            <select
              value={judgesCount}
              onChange={(event) => setJudgesCount(Number(event.target.value))}
              disabled={busy}
            >
              <option value={1}>{t("judgesOne")}</option>
              <option value={3}>{t("judgesPanel")}</option>
            </select>
          </label>
          <button onClick={() => void handleStart()} disabled={busy || !prompt.trim()}>
            {startLabel}
          </button>
        </div>
      </section>

      {errorMessage && <p className="error-line">{errorMessage}</p>}

      {view && (
        <>
          {view.source !== "live" && (
            <p className="muted run-note">
              {view.source === "import" ? t("importedRun") : t("historyRun")}:{" "}
              {view.prompt.slice(0, 120)}
            </p>
          )}
          <ArenaGrid
            tiles={view.tiles}
            winnerId={view.winnerId}
            live={Boolean(live)}
            revealAll={revealLosers}
          />
          <VerdictPanel
            winner={winner}
            responses={responses}
            verdicts={view.verdicts}
            errors={view.judgeErrors}
            canRevote={view.source !== "import" && !busy && Boolean(view.runId)}
            revoting={revoting}
            onRevote={() => void handleRevote()}
            live={Boolean(live)}
            revealAll={revealLosers}
            onToggleReveal={() => setRevealLosers((current) => !current)}
          />
        </>
      )}

      {history.length > 0 && (
        <section className="history">
          <h2>{t("history")}</h2>
          <ul>
            {history.map((item) => (
              <li key={item.id}>
                <button
                  className="link"
                  onClick={() => void handleOpenHistory(item.id)}
                  disabled={busy}
                >
                  {new Date(item.created_at).toLocaleString()} ·{" "}
                  {t("answersCount", { n: item.responses_count })} · {item.prompt.slice(0, 60)}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="status">{statusLine}</footer>
    </main>
  );
}
