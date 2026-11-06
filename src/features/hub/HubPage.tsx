import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { Select, type SelectOption } from '@/components/ui/Select';
import {
  filterHubRows,
  getHubOrganizations,
  getHubSummary,
  summarizeHubTotals,
  type HubOrganization,
  type HubSummary,
} from '@/services/api/hub';
import styles from './HubPage.module.scss';

const PERIODS = [1, 7, 30, 90] as const;
const number = (value: number) => new Intl.NumberFormat().format(value || 0);

export function HubPage({ refreshKey }: { refreshKey: number }) {
  const { t, i18n } = useTranslation();
  const [organizations, setOrganizations] = useState<HubOrganization[] | null>(null);
  const [organization, setOrganization] = useState('all');
  const [days, setDays] = useState<number>(30);
  const [summary, setSummary] = useState<HubSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [organizationError, setOrganizationError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getHubOrganizations(controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setOrganizations(items);
        setOrganizationError(null);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted)
          setOrganizationError(cause instanceof Error ? cause.message : t('hub.load_error'));
      });
    return () => controller.abort();
  }, [refreshKey, retry, t]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    getHubSummary(days, organization, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        setSummary(data);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setSummary(null);
          setError(cause instanceof Error ? cause.message : t('hub.load_error'));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [days, organization, refreshKey, retry, t]);

  const selectedName =
    organization === 'all'
      ? t('hub.all_organizations')
      : (organizations?.find((item) => item.id === organization)?.name ?? organization);
  const organizationOptions: SelectOption[] = useMemo(
    () => [
      { value: 'all', label: t('hub.all_organizations') },
      ...(organizations ?? []).map(({ id, name }) => ({ value: id, label: name })),
    ],
    [organizations, t]
  );
  const scoped =
    summary && organization !== 'all'
      ? {
          ...summary,
          models: filterHubRows(summary.models, organization),
          daily: filterHubRows(summary.daily, organization),
          health: filterHubRows(summary.health, organization),
        }
      : summary;
  const totals = summary ? summarizeHubTotals(summary.totals, organization) : undefined;
  const hasActivity = Boolean(scoped && (scoped.totals.length > 0 || scoped.models.length > 0));
  const dailyRows = scoped?.daily.reduce((result, row) => {
    const previous = result.get(row.day) ?? {
      day: row.day,
      client: 'all',
      requests: 0,
      total_tokens: 0,
    };
    previous.requests += row.requests;
    previous.total_tokens += row.total_tokens;
    result.set(row.day, previous);
    return result;
  }, new Map<string, { day: string; client: string; requests: number; total_tokens: number }>());
  const daily = dailyRows ? [...dailyRows.values()].sort((a, b) => a.day.localeCompare(b.day)) : [];
  const maxDaily = Math.max(1, ...daily.map((entry) => entry.total_tokens));

  const metricItems = [
    {
      key: 'reported_total',
      label: t('hub.reported_total'),
      value: totals?.total_tokens ?? 0,
      strong: true,
    },
    { key: 'input', label: t('hub.input_tokens'), value: totals?.input_tokens ?? 0 },
    { key: 'output', label: t('hub.output_tokens'), value: totals?.output_tokens ?? 0 },
    { key: 'cache_read', label: t('hub.cache_read'), value: totals?.cache_read_tokens ?? 0 },
    {
      key: 'cache_creation',
      label: t('hub.cache_creation'),
      value: totals?.cache_creation_tokens ?? 0,
    },
    { key: 'reasoning', label: t('hub.reasoning_tokens'), value: totals?.reasoning_tokens ?? 0 },
  ];

  return (
    <div className={styles.page}>
      <header className={styles.pageHeader}>
        <div>
          <div className={styles.kicker}>{t('hub.eyebrow')}</div>
          <h1>{t('hub.title')}</h1>
          <p>{t('hub.subtitle', { organization: selectedName })}</p>
        </div>
        <div className={styles.filters}>
          <label className={styles.filter}>
            <span>{t('hub.organization')}</span>
            <Select
              value={organization}
              options={organizationOptions}
              onChange={setOrganization}
              ariaLabel={t('hub.organization')}
            />
          </label>
          <div className={styles.periodGroup} role="group" aria-label={t('hub.period')}>
            <span>{t('hub.period')}</span>
            <div className={styles.periodButtons}>
              {PERIODS.map((period) => (
                <button
                  type="button"
                  key={period}
                  className={days === period ? styles.periodActive : ''}
                  aria-pressed={days === period}
                  onClick={() => setDays(period)}
                >
                  {t('hub.days', { count: period })}
                </button>
              ))}
            </div>
          </div>
        </div>
      </header>

      {(organizationError || error) && (
        <div className={styles.error} role="alert">
          {t('hub.load_error')}: {organizationError ?? error}
        </div>
      )}
      {loading && (
        <div className={styles.loading} aria-live="polite">
          {t('hub.loading')}
        </div>
      )}
      {!loading && !error && !organizationError && !hasActivity && (
        <Card className={styles.emptyCard}>
          <EmptyState title={t('hub.empty_title')} description={t('hub.empty_description')} />
        </Card>
      )}
      {!loading && !error && !organizationError && scoped && (
        <>
          <section className={styles.metrics} aria-label={t('hub.usage_summary')}>
            {metricItems.map((item) => (
              <Card
                key={item.key}
                className={`${styles.metricCard} ${item.strong ? styles.metricStrong : ''}`}
              >
                <div className={styles.metricLabel}>{item.label}</div>
                <div className={styles.metricValue}>{number(item.value)}</div>
                {(item.key === 'cache_read' || item.key === 'cache_creation') && (
                  <div className={styles.metricNote}>{t('hub.cache_note')}</div>
                )}
              </Card>
            ))}
            <Card className={styles.metricCard}>
              <div className={styles.metricLabel}>{t('hub.requests')}</div>
              <div className={styles.metricValue}>{number(totals?.requests ?? 0)}</div>
              <div className={styles.metricNote}>
                {t('hub.errors', { count: number(totals?.failed ?? 0) })}
              </div>
            </Card>
          </section>

          <section className={styles.panels}>
            <Card
              title={t('hub.daily_activity')}
              extra={
                <span className={styles.cardMeta}>{t('hub.days', { count: scoped.days })}</span>
              }
              className={styles.panel}
            >
              {daily.length === 0 ? (
                <div className={styles.noData}>{t('hub.no_activity')}</div>
              ) : (
                <div className={styles.chart} role="img" aria-label={t('hub.daily_chart_label')}>
                  {daily.map((entry, index) => (
                    <div
                      className={styles.barColumn}
                      key={`${entry.day}-${entry.client}-${index}`}
                      title={`${entry.day}: ${number(entry.total_tokens)}`}
                    >
                      <div className={styles.barTrack}>
                        <div
                          className={styles.bar}
                          style={{
                            height: `${Math.max(3, (entry.total_tokens / maxDaily) * 100)}%`,
                          }}
                        />
                      </div>
                      <span>
                        {new Date(`${entry.day}T00:00:00`).toLocaleDateString(i18n.language, {
                          day: '2-digit',
                          month: 'short',
                        })}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Card>
            <Card title={t('hub.collector_status')} className={styles.panel}>
              <div className={styles.statusList}>
                {(scoped.health ?? []).map((item) => (
                  <div className={styles.statusRow} key={item.client}>
                    <span
                      className={`${styles.statusDot} ${item.error ? styles.statusBad : ''}`}
                      aria-hidden="true"
                    />
                    <div className={styles.statusText}>
                      <b>
                        {organizations?.find((org) => org.id === item.client)?.name ?? item.client}
                      </b>
                      <span>
                        {item.error ||
                          (item.last_success
                            ? t('hub.last_success', {
                                date: new Date(item.last_success).toLocaleString(i18n.language),
                              })
                            : t('hub.never_collected'))}
                      </span>
                    </div>
                    <span className={styles.statusLabel}>
                      {item.error
                        ? t('hub.status_error')
                        : item.last_success
                          ? t('hub.status_ok')
                          : t('hub.status_waiting')}
                    </span>
                  </div>
                ))}
                {scoped.health.length === 0 && (
                  <div className={styles.noData}>{t('hub.no_collector_data')}</div>
                )}
              </div>
              {scoped.updated && (
                <div className={styles.updated}>
                  {t('hub.updated', {
                    date: new Date(scoped.updated).toLocaleString(i18n.language),
                  })}
                </div>
              )}
            </Card>
          </section>

          <Card title={t('hub.model_usage')} className={styles.tableCard}>
            {scoped.models.length === 0 ? (
              <div className={styles.noData}>{t('hub.no_activity')}</div>
            ) : (
              <div className={styles.tableWrap}>
                <table>
                  <thead>
                    <tr>
                      <th>{t('hub.organization')}</th>
                      <th>{t('hub.provider')}</th>
                      <th>{t('hub.model')}</th>
                      <th>{t('hub.requests')}</th>
                      <th>{t('hub.input_tokens')}</th>
                      <th>{t('hub.output_tokens')}</th>
                      <th>{t('hub.reported_total')}</th>
                      <th>{t('hub.errors_short')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scoped.models.map((row, index) => (
                      <tr key={`${row.client}-${row.provider}-${row.model}-${index}`}>
                        <td>
                          {organizations?.find((org) => org.id === row.client)?.name ?? row.client}
                        </td>
                        <td>{row.provider}</td>
                        <td className={styles.modelName}>{row.model}</td>
                        <td>{number(row.requests)}</td>
                        <td>{number(row.input_tokens)}</td>
                        <td>{number(row.output_tokens)}</td>
                        <td>{number(row.total_tokens)}</td>
                        <td>{number(row.failed)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </>
      )}
      {(error || organizationError) && (
        <button
          className={styles.retry}
          type="button"
          onClick={() => setRetry((value) => value + 1)}
        >
          {t('hub.retry')}
        </button>
      )}
    </div>
  );
}
