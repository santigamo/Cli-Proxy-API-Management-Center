import { HubPage } from './HubPage';
import { INLINE_LOGO_JPEG } from '@/assets/logoInline';
import { IconSidebarDashboard } from '@/components/ui/icons';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/Button';
import { useState } from 'react';
import { Select } from '@/components/ui/Select';
import { useLanguageStore } from '@/stores/useLanguageStore';
import { useThemeStore } from '@/stores/useThemeStore';
import { LANGUAGE_LABEL_KEYS, LANGUAGE_ORDER } from '@/utils/constants';
import type { Language, Theme } from '@/types';
import './hubLayout.scss';
import { HubOrganizationControl } from './HubSession';
import { useHubSession } from './hubSessionContext';

export function HubLayout() {
  const { t } = useTranslation();
  const { switching } = useHubSession();
  const [refreshKey, setRefreshKey] = useState(0);
  const language = useLanguageStore((state) => state.language);
  const setLanguage = useLanguageStore((state) => state.setLanguage);
  const theme = useThemeStore((state) => state.theme);
  const setTheme = useThemeStore((state) => state.setTheme);

  return (
    <div className="app-shell hub-shell">
      <div className="top-gradient-blur" aria-hidden="true" />
      <header className="main-header hub-header">
        <div className="hub-header-label">
          <span className="hub-header-mark" aria-hidden="true" />
          {t('hub.header')}
        </div>
        <div className="header-actions floating-actions">
          <HubOrganizationControl />
          <Select
            value={language}
            options={LANGUAGE_ORDER.map((value) => ({
              value,
              label: t(LANGUAGE_LABEL_KEYS[value]),
            }))}
            onChange={(value) => setLanguage(value as Language)}
            ariaLabel={t('language.switch')}
            size="sm"
            fullWidth={false}
          />
          <Select
            value={theme}
            options={(['auto', 'white', 'light', 'dark'] as Theme[]).map((value) => ({
              value,
              label: t(`theme.${value}`),
            }))}
            onChange={(value) => setTheme(value as Theme)}
            ariaLabel={t('theme.switch')}
            size="sm"
            fullWidth={false}
          />
          <Button variant="ghost" size="sm" onClick={() => setRefreshKey((n) => n + 1)}>
            {t('hub.refresh')}
          </Button>
        </div>
      </header>
      <div className="main-body">
        <aside className="sidebar hub-sidebar">
          <div className="sidebar-header">
            <div className="sidebar-brand" title="CLI Proxy API Management Center">
              <img src={INLINE_LOGO_JPEG} alt="CPAMC logo" className="sidebar-brand-logo" />
              <span className="sidebar-brand-text">
                <span className="sidebar-brand-title">CPAMC</span>
                <span className="sidebar-brand-subtitle">{t('sidebar.subtitle')}</span>
              </span>
            </div>
          </div>
          <nav className="nav-section" aria-label={t('hub.navigation')}>
            <div className="nav-group">
              <div className="nav-group-label">{t('nav_groups.observe')}</div>
              <div className="nav-item active" aria-current="page">
                <span className="nav-icon">
                  <IconSidebarDashboard size={18} />
                </span>
                <span className="nav-text">
                  <span className="nav-label">{t('hub.title')}</span>
                </span>
              </div>
            </div>
          </nav>
        </aside>
        <div className="content">
          <main className="main-content hub-main-content">
            {switching ? (
              <div className="hub-switching">{t('hub.switching')}</div>
            ) : (
              <HubPage refreshKey={refreshKey} />
            )}
          </main>
        </div>
      </div>
    </div>
  );
}
