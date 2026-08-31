module.exports = {
  apps: [{
    name: "bottle-bot",
    script: "bot.js",
    autorestart: true,
    max_restarts: 10,
    restart_delay: 5000,
    env: {
      NODE_ENV: "production"
    }
  }]
};
