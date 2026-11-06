export type HubOrganization = { id: string; name: string };
export type HubTotals = {
  client: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  reasoning_tokens: number;
  total_tokens: number;
  failed: number;
};
export type HubModel = HubTotals & { provider: string; model: string };
export type HubDaily = { day: string; client: string; requests: number; total_tokens: number };
export type HubHealth = { client: string; last_success: string | null; error: string | null };
export type HubSummary = {
  days: number;
  totals: HubTotals[];
  models: HubModel[];
  daily: HubDaily[];
  health: HubHealth[];
  updated: string | null;
};

type CacheInputUsage = { cacheRead: number; inputTotal: number; complete: boolean };

/** Match CLIProxyAPI v8.0.58 tokenAccountingSemanticsFor provider rules. */
export function cacheInputUsage(
  row: Pick<HubModel, 'provider' | 'input_tokens' | 'cache_read_tokens' | 'cache_creation_tokens'>
): CacheInputUsage {
  const provider = row.provider.trim().toLowerCase();
  const value = provider;
  const nonnegative = [row.input_tokens, row.cache_read_tokens, row.cache_creation_tokens].every(
    (token) => Number.isFinite(token) && token >= 0
  );
  if (!nonnegative) return { cacheRead: 0, inputTotal: 0, complete: false };
  let semantics: 'subset' | 'independent' | 'unknown' = 'unknown';
  if (provider === 'openai-compatibility' || provider.startsWith('openai-compatible-')) {
    semantics = 'subset';
  } else if (value.includes('claude') || value.includes('anthropic')) {
    semantics = 'independent';
  } else if (
    [
      'gemini',
      'aistudio',
      'antigravity',
      'vertex',
      'interaction',
      'openai',
      'codex',
      'xai',
      'grok',
      'kimi',
      'qwen',
      'deepseek',
      'openrouter',
    ].some((marker) => value.includes(marker))
  ) {
    semantics = 'subset';
  }

  if (semantics === 'unknown') {
    return {
      cacheRead: 0,
      inputTotal: 0,
      complete:
        row.input_tokens === 0 && row.cache_read_tokens === 0 && row.cache_creation_tokens === 0,
    };
  }
  return {
    cacheRead: row.cache_read_tokens,
    inputTotal:
      semantics === 'independent'
        ? row.input_tokens + row.cache_read_tokens + row.cache_creation_tokens
        : row.input_tokens,
    complete:
      semantics !== 'subset' ||
      row.cache_read_tokens + row.cache_creation_tokens <= row.input_tokens,
  };
}

export function cacheHitRate(
  rows: Array<
    Pick<HubModel, 'provider' | 'input_tokens' | 'cache_read_tokens' | 'cache_creation_tokens'>
  >
): number | null {
  const usage = rows.map(cacheInputUsage);
  if (usage.some((item) => !item.complete)) return null;
  const inputTotal = usage.reduce((sum, item) => sum + item.inputTotal, 0);
  return inputTotal > 0
    ? (usage.reduce((sum, item) => sum + item.cacheRead, 0) / inputTotal) * 100
    : null;
}

const ADDITIVE_TOTAL_FIELDS = [
  'requests',
  'input_tokens',
  'output_tokens',
  'cache_read_tokens',
  'cache_creation_tokens',
  'reasoning_tokens',
  'total_tokens',
  'failed',
] as const;

/** Sum provider-reported fields as reported; cache values remain independent subsets. */
export function summarizeHubTotals(rows: HubTotals[], organization: string): HubTotals {
  const selected =
    organization === 'all' ? rows : rows.filter((row) => row.client === organization);
  return ADDITIVE_TOTAL_FIELDS.reduce<HubTotals>(
    (result, field) => ({
      ...result,
      [field]: selected.reduce((sum, row) => sum + (row[field] || 0), 0),
    }),
    {
      client: organization,
      requests: 0,
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 0,
      failed: 0,
    }
  );
}

export function filterHubRows<T extends { client: string }>(rows: T[], organization: string): T[] {
  return organization === 'all' ? rows : rows.filter((row) => row.client === organization);
}

export const getHubSummaryUrl = (days: number, organization: string) => {
  const params = new URLSearchParams({ days: String(days), organization });
  return `/api/summary?${params.toString()}`;
};

export const getHubOrganizationApiBase = (organization: string, origin: string) =>
  `${origin.replace(/\/$/, '')}/organizations/${encodeURIComponent(organization)}`;

export const getHubLoginCredentials = (organization: string, origin: string) => ({
  apiBase: getHubOrganizationApiBase(organization, origin),
  managementKey: 'managed-on-server',
  rememberPassword: false,
});

export const isHubManagementApiBase = (apiBase: string, hubMode: boolean) =>
  hubMode && /\/organizations\/[^/]+\/v8\/management$/.test(apiBase);

export const hubManagementRequestHeaders = (apiBase: string, hubMode: boolean) =>
  isHubManagementApiBase(apiBase, hubMode) ? { 'X-Hub-Request': '1' } : {};

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, { method: 'GET', signal, credentials: 'omit' });
  if (!response.ok) throw new Error(`Hub request failed (${response.status})`);
  return (await response.json()) as T;
}

export const getHubOrganizations = (signal?: AbortSignal) =>
  getJson<HubOrganization[]>('/api/organizations', signal);

export const getHubSummary = (days: number, organization: string, signal?: AbortSignal) => {
  return getJson<HubSummary>(getHubSummaryUrl(days, organization), signal);
};
