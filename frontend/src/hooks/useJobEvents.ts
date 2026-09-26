/**
 * Server-sent job progress with `Last-Event-ID` recovery (spec 8.4).
 *
 * Disconnecting never affects the worker: the browser reconnects and resumes
 * after the last delivered sequence, and an unknown sequence makes the server
 * send a fresh job snapshot before live events.
 */

import { useEffect, useRef, useState } from "react";

import type { JobEvent } from "../api/types";

export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

interface Options {
  enabled?: boolean;
  onEvent?: (event: JobEvent) => void;
}

export function useJobEvents(jobId: string | null, options: Options = {}) {
  const { enabled = true, onEvent } = options;
  const [connection, setConnection] = useState<ConnectionState>("closed");
  const [lastEvent, setLastEvent] = useState<JobEvent | null>(null);
  const lastSequence = useRef<number>(0);
  const handlerRef = useRef(onEvent);
  handlerRef.current = onEvent;

  useEffect(() => {
    if (!jobId || !enabled) {
      setConnection("closed");
      return;
    }

    let source: EventSource | null = null;
    let retryTimer: number | undefined;
    let closed = false;
    let attempt = 0;

    const connect = () => {
      if (closed) return;
      setConnection(attempt === 0 ? "connecting" : "reconnecting");

      // EventSource cannot set headers, so the resume point travels as a query
      // parameter; the server accepts either form.
      const params = new URLSearchParams();
      if (lastSequence.current > 0) {
        params.set("lastEventId", String(lastSequence.current));
      }
      const query = params.toString();
      source = new EventSource(
        `/api/v1/jobs/${jobId}/events${query ? `?${query}` : ""}`,
        { withCredentials: true },
      );

      const handle = (raw: MessageEvent) => {
        try {
          const parsed = JSON.parse(raw.data) as JobEvent;
          if (typeof parsed.sequence === "number" && parsed.sequence > lastSequence.current) {
            lastSequence.current = parsed.sequence;
          }
          setLastEvent(parsed);
          handlerRef.current?.(parsed);
        } catch {
          // A malformed frame is ignored; the next poll or event recovers.
        }
      };

      source.onopen = () => {
        attempt = 0;
        setConnection("open");
      };
      for (const type of [
        "job.updated",
        "candidates.ready",
        "export.ready",
        "job.failed",
        "heartbeat",
      ]) {
        source.addEventListener(type, handle as EventListener);
      }
      source.onmessage = handle;

      source.onerror = () => {
        source?.close();
        if (closed) return;
        setConnection("reconnecting");
        attempt += 1;
        const delay = Math.min(15000, 1000 * 2 ** Math.min(attempt, 4));
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      source?.close();
      setConnection("closed");
    };
  }, [jobId, enabled]);

  return { connection, lastEvent };
}
