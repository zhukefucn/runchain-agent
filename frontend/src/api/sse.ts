export type StableEvent = {
  type: "run_started" | "token" | "agent_started" | "tool_call" | "tool_result" | "agent_completed" | "hitl_pending" | "complete" | "error";
  request_id: string;
  session_id: string;
  run_id: string;
  timestamp?: string;
  data: Record<string, unknown>;
};

function decodeFrame(frame: string): StableEvent | null {
  const data = frame
    .split(/\r\n|\r|\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") return null;
  return JSON.parse(data) as StableEvent;
}

function takeFrame(buffer: string): { frame: string; rest: string } | null {
  const match = /\r\n\r\n|\n\n|\r\r/.exec(buffer);
  if (!match || match.index === undefined) return null;
  return { frame: buffer.slice(0, match.index), rest: buffer.slice(match.index + match[0].length) };
}

export async function* parseSseStream(response: Response): AsyncGenerator<StableEvent> {
  if (!response.body) throw new Error("浏览器未提供流式响应体");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let extracted = takeFrame(buffer);
      while (extracted) {
        const event = decodeFrame(extracted.frame);
        buffer = extracted.rest;
        if (event) yield event;
        extracted = takeFrame(buffer);
      }
      if (done) break;
    }
    const trailing = decodeFrame(buffer);
    if (trailing) yield trailing;
  } finally {
    reader.releaseLock();
  }
}
