import type { App } from "@modelcontextprotocol/ext-apps";
import type {
  ListSessionsResponse,
  SessionDelta,
  SessionDetail,
} from "./types";

interface ToolCallResultLike {
  isError?: boolean;
  structuredContent?: unknown;
  content?: Array<{ type?: string; text?: string }>;
}

function extractPayload<T>(result: ToolCallResultLike): T {
  if (result.isError) {
    const msg =
      result.content?.find((c) => c.type === "text" && c.text)?.text ||
      "Server tool returned an error";
    throw new Error(msg);
  }
  if (result.structuredContent !== undefined && result.structuredContent !== null) {
    return result.structuredContent as T;
  }
  const textBlock = result.content?.find((c) => c.type === "text" && c.text);
  if (textBlock?.text) {
    return JSON.parse(textBlock.text) as T;
  }
  throw new Error("Empty tool response");
}

export class OrbitApi {
  constructor(private readonly app: App) {}

  async listSessions(query = "", limit = 80): Promise<ListSessionsResponse> {
    const res = await this.app.callServerTool({
      name: "orbit_list_sessions",
      arguments: { query, limit },
    });
    return extractPayload<ListSessionsResponse>(res as ToolCallResultLike);
  }

  async getSessionDetail(sessionId: string): Promise<SessionDetail | null> {
    const res = await this.app.callServerTool({
      name: "orbit_get_session_detail",
      arguments: { session_id: sessionId },
    });
    const data = extractPayload<{ found: boolean; detail?: SessionDetail | null }>(
      res as ToolCallResultLike,
    );
    return data.found && data.detail ? data.detail : null;
  }

  async pollSessionDelta(
    sessionId: string,
    afterCursor: number,
  ): Promise<SessionDelta | null> {
    const res = await this.app.callServerTool({
      name: "orbit_poll_session_delta",
      arguments: { session_id: sessionId, after_cursor: afterCursor },
    });
    const data = extractPayload<{ found: boolean; delta?: SessionDelta | null }>(
      res as ToolCallResultLike,
    );
    return data.found && data.delta ? data.delta : null;
  }
}
