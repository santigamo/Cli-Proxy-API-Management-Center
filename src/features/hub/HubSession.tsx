import type { PropsWithChildren } from 'react';
import { useTranslation } from 'react-i18next';
import { Select } from '@/components/ui/Select';
import { HubSessionContext, useHubSession } from './hubSessionContext';
import type { HubSessionValue } from './hubSessionContext';

export function HubSessionProvider({
  value,
  children,
}: PropsWithChildren<{ value: HubSessionValue }>) {
  return <HubSessionContext.Provider value={value}>{children}</HubSessionContext.Provider>;
}

export function HubOrganizationControl() {
  const { t } = useTranslation();
  const { organizations, organization, selectOrganization } = useHubSession();
  const options = [
    { value: 'all', label: t('hub.all_organizations') },
    ...organizations.map(({ id, name }) => ({ value: id, label: name })),
  ];

  return (
    <div className="hub-org-control">
      <Select
        value={organization ?? 'all'}
        options={options}
        onChange={selectOrganization}
        ariaLabel={t('hub.organization')}
        size="sm"
        fullWidth={false}
      />
    </div>
  );
}
