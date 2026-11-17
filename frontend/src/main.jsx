import { render } from 'preact'
import './index.css'
import { App } from './app.jsx'
import { registerServiceWorker } from './offline.js'

render(<App />, document.getElementById('app'))
registerServiceWorker()
