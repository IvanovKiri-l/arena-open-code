// Обёртки над API арены. База — относительный /api: в Docker его проксирует
// nginx, в dev — vite-прокси. VITE_API_URL из .env нужен только для
// нестандартных схем (фронт и бэк на разных хостах).
import type {
  Health,
  JudgeResultPayload,
  RunPayload,
  RunSummary,
  StreamEvent,
} from "./types";

const configured = (import.meta.env.VITE_API_URL as string | undefined)?.trim();

export const API_BASE = configured ? configured.replace(/\/+$/, "") : "/api";

export function apiUrl(path: string): string {
  return `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
}

async function asJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`HTTP ${response.status}${detail ? `: ${detail.slice(0, 300)}` : ""}`);
  }
  return (await response.json()) as T;
}

export async function getHealth(): Promise<Health> {
  return asJson<Health>(await fetch(apiUrl("/health")));
}

export async function startRun(
  prompt: string,
  options: { judges?: number; verify?: boolean } = {},
): Promise<{ run_id: string; status: string }> {
  const response = await fetch(apiUrl("/arena"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, judges: options.judges, verify: options.verify }),
  });
  return asJson(response);
}

export async function getRun(runId: string): Promise<RunPayload> {
  return asJson<RunPayload>(await fetch(apiUrl(`/arena/${runId}`)));
}

export async function listRuns(limit = 20): Promise<RunSummary[]> {
  const data = await asJson<{ runs: RunSummary[] }>(
    await fetch(apiUrl(`/runs?limit=${limit}`)),
  );
  return data.runs;
}

export async function rejudge(
  runId: string,
  options: { judges?: number } = {},
): Promise<JudgeResultPayload> {
  const response = await fetch(apiUrl(`/arena/${runId}/judge`), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ judges: options.judges }),
  });
  return asJson<JudgeResultPayload>(response);
}

export function exportRunUrl(runId: string): string {
  return apiUrl(`/arena/${runId}/export`);
}

// Живой поток событий забега (SSE). Возвращает функцию отписки.
export function openRunStream(
  runId: string,
  onEvent: (event: StreamEvent) => void,
): () => void {
  const source = new EventSource(apiUrl(`/arena/${runId}/stream`));
  let closed = false;
  let errorsWithoutData = 0;

  const close = () => {
    if (!closed) {
      closed = true;
      source.close();
    }
  };

  source.onmessage = (message) => {
    errorsWithoutData = 0;
    try {
      const event = JSON.parse(message.data) as StreamEvent;
      if (event.type === "end") {
        close();
      }
      onEvent(event);
    } catch {
      // не-JSON строка (например, keep-alive) — пропускаем
    }
  };

  source.onerror = () => {
    // Сервер закрыл поток после «end» — это штатно; но если данные так и
    // не пришли, после нескольких переподключений прекращаем попытки.
    errorsWithoutData += 1;
    if (errorsWithoutData >= 5) {
      close();
      onEvent({ type: "error", message: "поток событий оборван" });
      onEvent({ type: "end" });
    }
  };

  return close;
}
