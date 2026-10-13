import { useI18n } from "./i18n";
import type { ResponseRecord, VerdictRecord } from "./types";

type Props = {
  winner: ResponseRecord | null;
  responses: ResponseRecord[];
  verdicts: VerdictRecord[];
  errors: string[];
  canRevote: boolean;
  revoting: boolean;
  onRevote: () => void;
  live: boolean;
  revealAll: boolean;
  onToggleReveal: () => void;
};

export default function VerdictPanel({
  winner,
  responses,
  verdicts,
  errors,
  canRevote,
  revoting,
  onRevote,
  live,
  revealAll,
  onToggleReveal,
}: Props) {
  const { t } = useI18n();
  const hasResult = verdicts.length > 0 || errors.length > 0 || winner !== null;
  if (!hasResult) {
    return null;
  }

  // Суммарный балл каждого ответа по всем судьям.
  const totals = new Map<string, { total: number; count: number }>();
  for (const verdict of verdicts) {
    for (const score of verdict.scores ?? []) {
      const entry = totals.get(score.id) ?? { total: 0, count: 0 };
      entry.total += score.score;
      entry.count += 1;
      totals.set(score.id, entry);
    }
  }

  const byId = new Map(responses.map((response) => [response.id, response]));
  const ranking = [...totals.entries()]
    .flatMap(([id, entry]) => {
      const response = byId.get(id);
      return response ? [{ response, ...entry }] : [];
    })
    .sort((a, b) => b.total - a.total);

  return (
    <section className="verdict">
      <header className="verdict-head">
        <h2>{t("verdict")}</h2>
        <div className="verdict-actions">
          {live && winner !== null && (
            <button className="ghost" onClick={onToggleReveal}>
              {revealAll ? t("hideLosers") : t("showLosers")}
            </button>
          )}
          <button className="ghost" onClick={onRevote} disabled={!canRevote || revoting}>
            {revoting ? t("revoteBusy") : t("revote")}
          </button>
        </div>
      </header>

      {winner ? (
        <p className="verdict-winner">
          {t("winnerLine", { slot: winner.slot, profile: winner.profile })}
        </p>
      ) : (
        <p className="verdict-winner">{t("noWinner")}</p>
      )}

      {ranking.length > 0 && (
        <table className="scores">
          <thead>
            <tr>
              <th>{t("colSlot")}</th>
              <th>{t("colRole")}</th>
              <th>{t("colScore")}</th>
            </tr>
          </thead>
          <tbody>
            {ranking.map(({ response, total, count }) => (
              <tr
                key={response.id}
                className={
                  winner !== null && response.id === winner.id ? "score-winner" : undefined
                }
              >
                <td>#{response.slot}</td>
                <td>{response.profile}</td>
                <td>
                  {total.toFixed(1)}{" "}
                  <span className="muted">({t("judgesCountShort", { n: count })})</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {verdicts.map((verdict) => (
        <p className="judge-note" key={verdict.judge_index}>
          <b>{t("judgeNote", { n: verdict.judge_index + 1 })}</b> {verdict.rationale || "—"}
        </p>
      ))}
      {errors.map((error) => (
        <p className="judge-note judge-err" key={error}>
          ! {error}
        </p>
      ))}
    </section>
  );
}
