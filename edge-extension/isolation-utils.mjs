import {loopbackOrigin} from './session-utils.mjs';

export function isolationBootstrap(value) {
  const origin = loopbackOrigin(value);
  if (!origin) return null;
  const url = new URL(value);
  if (url.pathname !== '/edge-session-bridge' || url.search) return null;
  const parameters = new URLSearchParams(url.hash.slice(1));
  const instance_id = parameters.get('instance');
  const bootstrap_nonce = parameters.get('nonce');
  const valid = text => typeof text === 'string' && /^[A-Za-z0-9_-]{32,64}$/.test(text);
  if (!valid(instance_id) || !valid(bootstrap_nonce) || [...parameters.keys()].length !== 2) return null;
  return {origin, instance_id, bootstrap_nonce};
}

export function jobsForConnection(jobs, connection) {
  if (!Array.isArray(jobs)) return [];
  return jobs.filter(job => connection?.instance_id ? job.instance_id === connection.instance_id : !job.instance_id);
}

export function jobIdentity(job) {
  return {record_id:job.record_id, request_id:job.request_id, ...(job.instance_id?{instance_id:job.instance_id}:{})};
}
