import type { ResponseRecord } from "./types";

export type Tile = {
  slot: number;
  text: string;
  record: ResponseRecord | null;
};

type Props = {
  tiles: Tile[];
  winnerId: string | null;
};

export default function ArenaGrid({ tiles, winnerId }: Props) {
  return (
    <section className="grid">
      {tiles.map((tile) => {
        const record = tile.record;
        const streaming = record === null && tile.text.length > 0;
        const state = record ? record.status : streaming ? "streaming" : "idle";
        const isWinner = record !== null && record.id === winnerId;
        return (
          <article
            key={tile.slot}
            className={`tile tile-${state}${isWinner ? " tile-winner" : ""}`}
          >
            <header className="tile-head">
              <span className="tile-num">#{tile.slot}</span>
              <span className="tile-meta">
                {record
                  ? `${record.profile} · t=${record.temperature}`
                  : streaming
                    ? "стрим…"
                    : "ожидание"}
              </span>
            </header>
            <p className="tile-body">{tile.text || "…"}</p>
            {record?.error && <p className="tile-err">{record.error}</p>}
            <footer className="tile-foot">
              {record && record.status === "ok" && (
                <>
                  {record.latency_ms !== null && <span>{record.latency_ms} мс</span>}
                  {record.tokens !== null && <span>{record.tokens} ток.</span>}
                </>
              )}
              {record && record.status !== "ok" && <span>{record.status}</span>}
            </footer>
          </article>
        );
      })}
    </section>
  );
}
