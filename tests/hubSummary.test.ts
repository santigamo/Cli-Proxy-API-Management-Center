import { describe, expect, test } from 'bun:test';
import {
  filterHubRows,
  getHubSummaryUrl,
  summarizeHubTotals,
  type HubTotals,
} from '../src/services/api/hub';

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
});
