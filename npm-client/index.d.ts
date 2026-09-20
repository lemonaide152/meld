/** meld — ephemeral context bridge SDK */

export interface MeldResponse {
  code: string;
  url: string;
  owner_url?: string;
  owner_token: string;
  context_a: string;
  resolved: boolean;
}

export interface MeldView {
  code: string;
  context_a: string;
  context_b: string | null;
  resolved: boolean;
  resolved_at?: string;
}

export interface MeldResult {
  code: string;
  context_a: string;
  context_b: string;
  resolved: boolean;
  owner_token: string;
  responder_pubkey?: string;
  signature?: string;
}

export interface MeldOptions {
  /** Override the base URL (defaults to https://meld.lemonaide152.workers.dev) */
  baseUrl?: string;
  /** API key for agent tier (Bearer auth) */
  apiKey?: string;
}

/** Create a new meld with your context */
export declare function create(context: string, opts?: MeldOptions): Promise<MeldResponse>;

/** View a meld by code (returns context_a, and context_b if resolved) */
export declare function view(code: string, opts?: MeldOptions): Promise<MeldView>;

/** Resolve a meld with your answer */
export declare function resolve(code: string, context: string, opts?: MeldOptions): Promise<MeldResponse>;

/** Read the merged result (requires owner_token from create) */
export declare function result(code: string, ownerToken: string, opts?: MeldOptions): Promise<MeldResult>;
