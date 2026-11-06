import { describe, expect, test } from 'bun:test';
import {
  filterHubRows,
  getHubLoginCredentials,
  getHubSummaryUrl,
  hubManagementRequestHeaders,
  summarizeHubTotals,
  type HubTotals,
} from '../src/services/api/hub';
import { buildModelsRequest } from '../src/services/api/models';

const row = (client: string, partial: Partial<HubTotals>): HubTotals => ({
  client,
  requests: 0,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_creation_tokens: 0,
  reasoning_tokens: 0,
  total_tokens: 0,
  failed: 0,
  ...partial,
});

describe('hub summary aggregation', () => {
  const fixture = [
    row('oasis', {
      requests: 3,
      input_tokens: 10,
      output_tokens: 4,
      cache_read_tokens: 6,
      total_tokens: 12,
      failed: 1,
    }),
    row('stradem', {
      requests: 5,
      input_tokens: 20,
      output_tokens: 8,
      cache_read_tokens: 9,
      total_tokens: 25,
      failed: 2,
    }),
  ];

  test('all organizations adds reported fields while keeping cached tokens separate', () => {
    expect(summarizeHubTotals(fixture, 'all')).toEqual({
      client: 'all',
      requests: 8,
      input_tokens: 30,
      output_tokens: 12,
      cache_read_tokens: 15,
      cache_creation_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 37,
      failed: 3,
    });
  });

  test('an organization selection excludes every other client row', () => {
    expect(summarizeHubTotals(fixture, 'stradem')).toMatchObject({
      client: 'stradem',
      requests: 5,
      input_tokens: 20,
      total_tokens: 25,
      failed: 2,
    });
    expect(
      filterHubRows(
        [
          { client: 'oasis', model: 'a' },
          { client: 'stradem', model: 'b' },
        ],
        'stradem'
      )
    ).toEqual([{ client: 'stradem', model: 'b' }]);
  });

  test('summary query carries the selected organization and period', () => {
    expect(getHubSummaryUrl(7, 'oasis')).toBe('/api/summary?days=7&organization=oasis');
  });

  test('native session targets the scoped proxy with only the nonsecret placeholder', () => {
    expect(getHubLoginCredentials('stradem', 'https://hub.example')).toEqual({
      apiBase: 'https://hub.example/organizations/stradem',
      managementKey: 'managed-on-server',
      rememberPassword: false,
    });
    expect(
      hubManagementRequestHeaders('https://hub.example/organizations/stradem/v8/management', true)
    ).toEqual({ 'X-Hub-Request': '1' });
    expect(hubManagementRequestHeaders('https://hub.example/v8/management', true)).toEqual({});
  });

  test('Hub model discovery uses the scoped server route and never forwards browser API keys', () => {
    const request = buildModelsRequest(
      'https://hub.example/organizations/oasis',
      'provider-secret-fixture',
      { Authorization: 'Bearer provider-secret-fixture' },
      true
    );
    expect(request).toEqual({
      endpoint: 'https://hub.example/organizations/oasis/v1/models',
      headers: { 'X-Hub-Request': '1' },
    });
    expect(JSON.stringify(request)).not.toContain('provider-secret-fixture');
  });

  test('standalone model discovery retains its original API-key behavior', () => {
    expect(buildModelsRequest('https://provider.example/v1', 'key-fixture', {}, false)).toEqual({
      endpoint: 'https://provider.example/v1/models',
      headers: { Authorization: 'Bearer key-fixture' },
    });
  });
});
