import { initVisualization } from './visualization.js';
const scene = initVisualization(document.getElementById('visual-canvas'));
window.updateScene = (snapshot) => {
  scene?.update({ ...snapshot, tasks: new Map(snapshot.tasks.map(task => [task.task_id, task])) });
};
window.sceneVisible = value => scene?.visible(value);
document.addEventListener('visibilitychange', () => scene?.visible(!document.hidden));
window.addEventListener('pagehide', () => scene?.visible(false));
window.sceneReady = !!scene;
