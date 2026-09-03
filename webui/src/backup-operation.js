const ACTIVE_STATES = new Set(['requested', 'launching', 'running'])

/**
 * Return the one backup operation whose progress belongs on screen.
 *
 * The API status wins once the request has been published. Before that response arrives,
 * the local selection keeps only the clicked archive busy. A global `restore` flag made every
 * row claim it was being restored, even though the backend always operates on one exact name.
 */
export function activeBackupOperation(local, remote) {
  if (remote && ACTIVE_STATES.has(remote.state)) {
    return {
      action: remote.action || '',
      backupName: remote.backup_name || '',
    }
  }
  if (local?.action) {
    return {
      action: local.action,
      backupName: local.backupName || '',
    }
  }
  return { action: '', backupName: '' }
}

export function backupOperationRunning(operation) {
  return !!operation && ACTIVE_STATES.has(operation.state)
}
