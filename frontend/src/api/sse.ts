export type StableEvent = {
  type: "token" | "agent_started" | "tool_call" | "tool_result" | "agent_completed" | "hitl_pending" | "complete" | "error";
  request_id: string;
  session_id: string;
  run_id: string;
  timestamp?: string;
  data: Record<string, unknown>;
};

function decodeFrame(frame: string): StableEvent | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data || data === "[DONE]") return null;
  return JSON.parse(data) as StableEvent;
}

export async function* parseSseStream(response: Response): AsyncGenerator<StableEvent> {
  if (!response.body) throw new Error("浏览器未提供流式响应体");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const event = decodeFrame(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (event) yield event;
        boundary = buffer.indexOf("\n\n");
      }
      if (done) break;
    }
    const trailing = decodeFrame(buffer);
    if (trailing) yield trailing;
  } finally {
    reader.releaseLock();
  }
}
