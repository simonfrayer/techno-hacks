# Welcome to your Lovable project

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Open your project in the [Lovable editor](https://lovable.dev) and keep building.

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: connect the project to GitHub and every change made in Lovable is committed straight to your repository.
- **Full ownership**: this code is yours. Push to your repository and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
npm i
npm run dev
```

## MedTracker data

The app reads records and climate readings from the local MedTracker HTTP API.
Start the API from the repository root with `uv run techno-hacks serve`. The
development app defaults to `http://localhost:8080` and refreshes the data
every 15 seconds. The API permits browser requests from `localhost` and
loopback IP origins on any port. For an origin on another host, set
`MEDTRACKER_WEB_ORIGINS` (comma-separated) on the API server.
To use a different API host, set
`VITE_MEDTRACKER_API_URL` in the web app environment (for example,
`http://192.168.1.20:8080`). The API must be reachable from the browser.

## Built with

- TanStack Start
- TypeScript
- React
- Tailwind CSS
