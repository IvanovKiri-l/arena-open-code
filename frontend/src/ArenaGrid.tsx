import type { CSSProperties } from "react";
import { useI18n } from "./i18n";
import type { ResponseRecord } from "./types";

export type Tile = {
  slot: number;
  text: string;
  record: ResponseRecord | null;
};

type Props = {
  tiles: Tile[];
  winnerId: string | null;
  /** Живой забег: включаем анимации — победитель «выпрыгивает», проигравшие рассыпаются. */
  live: boolean;
  /** Пользователь попросил показать проигравших обратно. */
  revealAll: boolean;
  /** Победитель занял весь грид (после того как проигравшие растворились). */
  hero: boolean;
  /** Клик по ответу: раскрыть и предложить скачать. */
  onOpen: (record: ResponseRecord) => void;
};

type Particle = { x: number; y: number; dx: number; dy: number; delay: number };

// Детерминированно генерируем частицы по номеру слота, чтобы они
// не пересоздавались на каждом рендере.
function particlesFor(slot: number): Particle[] {
  let state = (slot * 2654435761) % 4294967296;
  const rnd = (): number => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
  return Array.from({ length: 16 }, () => ({
    x: 12 + rnd() * 76,
    y: 12 + rnd() * 76,
    dx: (rnd() - 0.5) * 180,
    dy: (rnd() - 0.5) * 160,
    delay: rnd() * 320,
  }));
}

const STATUS_KEYS: Record<string, "statusError" | "statusDisqualified"> = {
  error: "statusError",
  disqualified: "statusDisqualified",
};

export default function ArenaGrid({ tiles, winnerId, live, revealAll, hero, onOpen }: Props) {
  const { t } = useI18n();

  return (
    <section className={`grid${hero ? " grid-hero" : ""}`}>
      {tiles.map((tile, index) => {
        const record = tile.record;
        const isWinner = record !== null && record.id === winnerId;
        const streaming = record === null && tile.text.length > 0;
        const state = record ? record.status : streaming ? "streaming" : "idle";
        const crumble = live && !revealAll && winnerId !== null && !isWinner;
        const heroTile = hero && isWinner;
        const hidden = hero && !isWinner;
        const statusKey = record && record.status !== "ok" ? STATUS_KEYS[record.status] : undefined;

        const classes = [
          "tile",
          `tile-${state}`,
          isWinner ? "tile-winner" : "",
          isWinner && live && !hero ? "tile-winner-pop" : "",
          heroTile ? "tile-hero" : "",
          crumble ? "tile-crumbling" : "",
          hidden ? "tile-hidden" : "",
          record ? "tile-clickable" : "",
        ]
          .filter(Boolean)
          .join(" ");

        return (
          <article
            key={tile.slot}
            className={classes}
            style={crumble ? ({ "--delay": `${index * 45}ms` } as CSSProperties) : undefined}
            onClick={record ? () => onOpen(record) : undefined}
            title={record ? t("tileOpenHint") : undefined}
          >
            {crumble && (
              <span className="particles" aria-hidden="true">
                {particlesFor(tile.slot).map((particle, particleIndex) => (
                  <span
                    key={particleIndex}
                    className="particle"
                    style={
                      {
                        "--x": `${particle.x}%`,
                        "--y": `${particle.y}%`,
                        "--dx": `${particle.dx}px`,
                        "--dy": `${particle.dy}px`,
                        "--delay": `${particle.delay}ms`,
                      } as CSSProperties
                    }
                  />
                ))}
              </span>
            )}
            <header className="tile-head">
              <span className="tile-num">
                #{tile.slot}
                {isWinner ? " ★" : ""}
              </span>
              <span className="tile-meta">
                {record
                  ? `${record.profile} · t=${record.temperature}`
                  : streaming
                    ? t("tileStreaming")
                    : t("tileWaiting")}
              </span>
            </header>
            <p className="tile-body">{tile.text || "…"}</p>
            {heroTile && <p className="tile-hint">{t("heroHint")}</p>}
            {record?.error && <p className="tile-err">{record.error}</p>}
            <footer className="tile-foot">
              {record && record.status === "ok" && (
                <>
                  {record.latency_ms !== null && (
                    <span>{t("latencyShort", { n: record.latency_ms })}</span>
                  )}
                  {record.tokens !== null && (
                    <span>{t("tokensShort", { n: record.tokens })}</span>
                  )}
                </>
              )}
              {record && record.status !== "ok" && (
                <span>{statusKey ? t(statusKey) : record.status}</span>
              )}
            </footer>
          </article>
        );
      })}
    </section>
  );
}
