import { useEffect } from "react";
import { useI18n } from "./i18n";
import type { ResponseRecord } from "./types";

type Props = {
  record: ResponseRecord;
  isWinner: boolean;
  runId: string;
  onClose: () => void;
};

export default function AnswerModal({ record, isWinner, runId, onClose }: Props) {
  const { t } = useI18n();

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const text = record.output ?? "";

  const download = (kind: "txt" | "md") => {
    const lines = [
      `run: ${runId}`,
      `slot: ${record.slot}`,
      `profile: ${record.profile}`,
      `temperature: ${record.temperature}`,
      record.latency_ms !== null ? `latency_ms: ${record.latency_ms}` : null,
      record.tokens !== null ? `tokens: ${record.tokens}` : null,
      `status: ${record.status}`,
    ].filter((line): line is string => line !== null);

    const body =
      kind === "md"
        ? `# Arena — answer #${record.slot}${isWinner ? " (winner)" : ""}\n\n` +
          lines.map((line) => `- ${line}`).join("\n") +
          `\n\n---\n\n${text}\n`
        : `Arena — answer #${record.slot}${isWinner ? " (winner)" : ""}\n` +
          lines.join("\n") +
          `\n\n${text}\n`;

    const blob = new Blob([body], {
      type: kind === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    const shortRun = runId ? runId.slice(0, 8) : "run";
    link.download = `arena-${shortRun}-slot-${record.slot}${isWinner ? "-winner" : ""}.${kind}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <h2>
            {isWinner && <span className="badge">★ {t("winnerBadge")}</span>}
            {t("answerTitle", { slot: record.slot })}
          </h2>
          <button className="ghost" onClick={onClose} aria-label={t("close")}>
            ✕
          </button>
        </header>

        <p className="modal-meta">
          [{record.profile}] · t={record.temperature}
          {record.latency_ms !== null && ` · ${t("latencyShort", { n: record.latency_ms })}`}
          {record.tokens !== null && ` · ${t("tokensShort", { n: record.tokens })}`}
        </p>
        {record.error && <p className="modal-error">{record.error}</p>}

        <pre className="modal-text">{text || "…"}</pre>

        <footer className="modal-actions">
          <button className="ghost" onClick={() => download("txt")}>
            {t("downloadTxt")}
          </button>
          <button className="ghost" onClick={() => download("md")}>
            {t("downloadMd")}
          </button>
        </footer>
      </div>
    </div>
  );
}
