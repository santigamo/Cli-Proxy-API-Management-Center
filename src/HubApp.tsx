import { useEffect } from 'react';
import { RouterProvider, createHashRouter } from 'react-router-dom';
import { HubLayout } from '@/features/hub/HubLayout';
import { useLanguageStore } from '@/stores/useLanguageStore';
import { useThemeStore } from '@/stores/useThemeStore';

const router = createHashRouter([{ path: '*', element: <HubLayout /> }]);

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
