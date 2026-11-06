import { createContext, useContext } from 'react';
import type { HubOrganization } from '@/services/api/hub';

export interface HubSessionValue {
  organizations: HubOrganization[];
  organization: string | null;
  switching: boolean;
  error: string | null;
  selectOrganization: (id: string) => void;
}

export const HubSessionContext = createContext<HubSessionValue | null>(null);

export function useHubSession() {
  const session = useContext(HubSessionContext);
  if (!session) throw new Error('Hub session is unavailable outside HubSessionProvider');
  return session;
}
