import { initVisualization } from './visualization.js';
const scene = initVisualization(document.getElementById('visual-canvas'));
window.updateScene = (snapshot) => {
  scene?.update({ ...snapshot, tasks: new Map(snapshot.tasks.map(task => [task.task_id, task])) });
};
if (window.sceneSnapshot) window.updateScene(window.sceneSnapshot);
window.sceneVisible = value => scene?.visible(value);
document.addEventListener('visibilitychange', () => scene?.visible(!document.hidden));
window.addEventListener('pagehide', () => scene?.visible(false));
window.sceneReady = !!scene;
