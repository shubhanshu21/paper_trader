import { redirect } from 'react-router-dom';
import { api } from '../api';

// Runs before the app shell renders — redirects to /login if panel auth
// is enabled and there's no valid session. Own file (not a component) so
// index.tsx's route config file exports components only.
export async function authLoader() {
  try {
    const health = await api.checkHealth();
    const isEnabled = health.auth === 'enabled';

    if (isEnabled) {
      try {
        await api.me();
        return null;
      } catch {
        return redirect('/login');
      }
    }
    return null;
  } catch {
    return null;
  }
}
