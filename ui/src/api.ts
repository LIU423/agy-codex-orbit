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

  async getSessionDetail(
    sessionId: string,
  ): Promise<{ detail: SessionDetail; metaTs: string } | null> {
    const res = await this.app.callServerTool({
      name: "orbit_get_session_detail",
      arguments: { session_id: sessionId },
    });
    const data = extractPayload<{
      found: boolean;
      meta_ts?: string;
      detail?: SessionDetail | null;
    }>(res as ToolCallResultLike);
    if (!data.found || !data.detail) return null;
    return { detail: data.detail, metaTs: data.meta_ts ?? "" };
  }

  async pollSessionDelta(
    sessionId: string,
    afterCursor: number,
    afterMetaTs: string,
  ): Promise<{ delta: SessionDelta; metaTs: string } | null> {
    const res = await this.app.callServerTool({
      name: "orbit_poll_session_delta",
      arguments: {
        session_id: sessionId,
        after_cursor: afterCursor,
        after_meta_ts: afterMetaTs,
      },
    });
    const data = extractPayload<{
      found: boolean;
      meta_ts?: string;
      delta?: SessionDelta | null;
    }>(res as ToolCallResultLike);
    if (!data.found || !data.delta) return null;
    return { delta: data.delta, metaTs: data.meta_ts ?? "" };
  }
}
