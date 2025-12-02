'use client';

import { createContext, useContext, useMemo, useEffect, useState } from 'react';
import { RoomContext } from '@livekit/components-react';
import { APP_CONFIG_DEFAULTS, type AppConfig } from '@/app-config';
import { useRoom } from '@/hooks/useRoom';

const SessionContext = createContext<{
  appConfig: AppConfig;
  isSessionActive: boolean;
  startSession: () => void;
  endSession: () => void;
  username: string | null;        // ⭐ ADDED
}>({
  appConfig: APP_CONFIG_DEFAULTS,
  isSessionActive: false,
  startSession: () => {},
  endSession: () => {},
  username: null,                 // ⭐ ADDED DEFAULT
});

interface SessionProviderProps {
  appConfig: AppConfig;
  children: React.ReactNode;
}

export const SessionProvider = ({ appConfig, children }: SessionProviderProps) => {
  const { room, isSessionActive, startSession, endSession } = useRoom(appConfig);

  const [username, setUsername] = useState<string | null>(null);   // ⭐ ADDED STATE

  // ⭐ FETCH USER NAME FROM BACKEND
  useEffect(() => {
    async function loadName() {
      try {
        const res = await fetch("/api/participant/latest");
        const data = await res.json();
        if (data?.name) setUsername(data.name);
      } catch (err) {
        console.error("Failed to load username:", err);
      }
    }
    loadName();
  }, []);

  const contextValue = useMemo(
    () => ({
      appConfig,
      isSessionActive,
      startSession,
      endSession,
      username,                 // ⭐ EXPOSE TO CONTEXT
    }),
    [appConfig, isSessionActive, startSession, endSession, username]
  );

  return (
    <RoomContext.Provider value={room}>
      <SessionContext.Provider value={contextValue}>
        {children}
      </SessionContext.Provider>
    </RoomContext.Provider>
  );
};

export function useSession() {
  return useContext(SessionContext);
}
