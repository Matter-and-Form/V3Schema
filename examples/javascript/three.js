const WebSocket = require('ws');

function THREE(url) { this.url = url; }

THREE.prototype.connect = function() {
    this.ws = new WebSocket(this.url);
    return new Promise((resolve, reject) => {
        this.ws.onopen = resolve;
        this.ws.onclose = reject;
    });
};

THREE.prototype.send = function(task) {
    return new Promise((resolve, reject) => {
        this.ws.onmessage = event => {
            try {
                const response = JSON.parse(event.data);
                if (!response.Task || response.Task.Index !== task.Index) return;
                if (response.Task.State === 'Failed') reject(response.Task);
                if (response.Task.State === 'Completed') resolve(response.Task.Output ?? {});
            } catch (error) { reject(error); }
        };
        this.ws.onerror = error => reject(error);
        this.ws.send(JSON.stringify({ Task: task }));
    });
};

module.exports = THREE;
