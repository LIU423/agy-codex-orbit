import type { App } from "@modelcontextprotocol/ext-apps";
import type {
  CancelDelegationResult,
  ListSessionsResponse,
  ProfileOverviewResponse,
  QuotaStatus,
  RoutingMode,
  SessionDelta,
  SessionDetail,
  UiLocale,
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

  async listSessions(query = "", limit = 100): Promise<ListSessionsResponse> {
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

  async cancelDelegation(
    sessionId: string,
    runId?: string | null,
  ): Promise<CancelDelegationResult> {
    const res = await this.app.callServerTool({
      name: "orbit_cancel_delegation",
      arguments: {
        session_id: sessionId,
        run_id: runId ?? undefined,
      },
    });
    return extractPayload<CancelDelegationResult>(res as ToolCallResultLike);
  }

  async getQuota(forceRefresh = false): Promise<QuotaStatus> {
    const res = await this.app.callServerTool({
      name: "orbit_get_quota",
      arguments: { force_refresh: forceRefresh },
    });
    return extractPayload<QuotaStatus>(res as ToolCallResultLike);
  }

  async setRoutingMode(
    routingMode: RoutingMode,
  ): Promise<{ routing_mode: RoutingMode; routing_mode_label: string }> {
    const res = await this.app.callServerTool({
      name: "orbit_set_routing_mode",
      arguments: { routing_mode: routingMode },
    });
    return extractPayload<{ routing_mode: RoutingMode; routing_mode_label: string }>(
      res as ToolCallResultLike,
    );
  }

  async setUiLocale(uiLocale: UiLocale): Promise<{ ui_locale: UiLocale }> {
    const res = await this.app.callServerTool({
      name: "orbit_set_ui_locale",
      arguments: { ui_locale: uiLocale },
    });
    return extractPayload<{ ui_locale: UiLocale }>(res as ToolCallResultLike);
  }

  async getProfileOverview(projectDir = ""): Promise<ProfileOverviewResponse> {
    const res = await this.app.callServerTool({
      name: "orbit_get_overview",
      arguments: { project_dir: projectDir },
    });
    return extractPayload<ProfileOverviewResponse>(res as ToolCallResultLike);
  }
}
