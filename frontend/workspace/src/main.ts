import { mount } from 'svelte';
import App from './App.svelte';
import './styles/tokens.css';
import './styles/app.css';

// The server fills these in (web_ui._render_workspace). Under `npm run dev` the
// placeholders stay, so the environment comes from ?env= instead.
function bodyData(name: string): string {
  const value = document.body.dataset[name] || '';
  return value.startsWith('__') ? '' : value;
}

const envName = bodyData('scopedEnv') || new URLSearchParams(location.search).get('env') || '';
// 'share': a share-link visitor. 'operator': a team member previewing.
const viewer = bodyData('viewer') === 'operator' ? 'operator' : 'share';
// White label: the operator's brand replaces the product name and logo.
const brand = {
  name: bodyData('brandName') || 'Oduflow',
  customLogo: bodyData('logoClass') === 'custom-logo',
  assetVersion: bodyData('assetVersion'),
};

mount(App, {
  target: document.getElementById('app')!,
  props: { envName, viewer, brand },
});
