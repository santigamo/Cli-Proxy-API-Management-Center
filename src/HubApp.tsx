import { useCallback, useEffect, useRef, useState } from 'react';
import { RouterProvider, createHashRouter } from 'react-router-dom';
import { HubLayout } from '@/features/hub/HubLayout';
import { HubOrganizationControl, HubSessionProvider } from '@/features/hub/HubSession';
import type { HubSessionValue } from '@/features/hub/hubSessionContext';
import { MainLayout } from '@/components/layout/MainLayout';
import {
  getHubLoginCredentials,
  getHubOrganizations,
  type HubOrganization,
} from '@/services/api/hub';
import { useAuthStore } from '@/stores/useAuthStore';
import { useLanguageStore } from '@/stores/useLanguageStore';
import { useThemeStore } from '@/stores/useThemeStore';
import { useTranslation } from 'react-i18next';

function HubRoot() {
  const { t } = useTranslation();
  const [organizations, setOrganizations] = useState<HubOrganization[]>([]);
  const [organizationsLoaded, setOrganizationsLoaded] = useState(false);
  const [organization, setOrganization] = useState<string | null>(null);
  const [switching, setSwitching] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const sequence = useRef(0);
  const startup = useRef(false);
  const login = useAuthStore((state) => state.login);
  const logout = useAuthStore((state) => state.logout);

  const selectOrganization = useCallback(
    async (nextOrganization: string) => {
      if (nextOrganization === organization && !switching) return;
      const requestID = ++sequence.current;
      setSwitching(true);
      setError(null);
      setOrganization(null);
      logout();

      if (nextOrganization === 'all') {
        setOrganization('all');
        setSwitching(false);
        return;
      }

      try {
        await login(getHubLoginCredentials(nextOrganization, window.location.origin));
        if (sequence.current === requestID) setOrganization(nextOrganization);
      } catch (cause: unknown) {
        if (sequence.current === requestID) {
          logout();
          setOrganization('all');
          setError(cause instanceof Error ? cause.message : t('hub.load_error'));
        }
      } finally {
        if (sequence.current === requestID) setSwitching(false);
      }
    },
    [login, logout, organization, switching, t]
  );

  useEffect(() => {
    const controller = new AbortController();
    getHubOrganizations(controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) {
          setOrganizations(items);
          setOrganizationsLoaded(true);
        }
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setOrganizationsLoaded(true);
        setOrganization('all');
        setSwitching(false);
        setError(cause instanceof Error ? cause.message : t('hub.load_error'));
      });
    return () => controller.abort();
  }, [t]);

  useEffect(() => {
    if (!organizationsLoaded || startup.current) return;
    startup.current = true;
    if (organizations.length > 0) {
      void selectOrganization(organizations[0].id);
    } else {
      setOrganization('all');
      setSwitching(false);
    }
  }, [organizations, organizationsLoaded, selectOrganization]);

  useEffect(() => {
    const onUnauthorized = () => {
      sequence.current += 1;
      setOrganization('all');
      setSwitching(false);
      setError(t('hub.session_expired'));
    };
    window.addEventListener('unauthorized', onUnauthorized);
    return () => window.removeEventListener('unauthorized', onUnauthorized);
  }, [t]);

  const value: HubSessionValue = {
    organizations,
    organization,
    switching,
    error,
    selectOrganization: (id) => void selectOrganization(id),
  };

  return (
    <HubSessionProvider value={value}>
      {!organizationsLoaded || switching || organization === null ? (
        <div className="hub-initializing" role="status">
          {t('hub.loading')}
        </div>
      ) : organization === 'all' ? (
        <HubLayout />
      ) : (
        <MainLayout hubControl={<HubOrganizationControl />} hubMode />
      )}
    </HubSessionProvider>
  );
}

const router = createHashRouter([{ path: '*', element: <HubRoot /> }]);

export default function HubApp() {
  const initializeTheme = useThemeStore((state) => state.initializeTheme);
  const language = useLanguageStore((state) => state.language);
  const setLanguage = useLanguageStore((state) => state.setLanguage);

  useEffect(() => initializeTheme(), [initializeTheme]);
  useEffect(() => setLanguage(language), [language, setLanguage]);

  useEffect(() => {
    document.documentElement.lang = language;
  }, [language]);

  return <RouterProvider router={router} />;
}
