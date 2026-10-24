import { renderMarkdown } from './markdown.js';
window.showMarkdown = text => renderMarkdown(document.querySelector('#content'), text);
