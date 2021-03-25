const app = {
	data: function() {
		return {
			computer_control: true,
			active_users: 0,
			filament_status_message: ""
		};
	},
	methods: {
		filamentOn: function() {
			var self = this;
			axios.get("/filament-on").then(function(response) {
				self.filament_status_message = response.data;
			})
			.catch(function(error) {
				if (error.response === undefined) {
					self.filament_status_message = "Failed to communicate with server.";
				}
				else {
					self.filament_status_message = error.response.data;
				}
			});
		},
		filamentOff: function() {
			var self = this;
			axios.get("/filament-off").then(function(response) {
				self.filament_status_message = response.data;
			})
			.catch(function(error) {
				if (error.response === undefined) {
					self.filament_status_message = "Failed to communicate with server.";
				}
				else {
					self.filament_status_message = error.response.data;
				}
			});
		}
	},
	mounted: function() {
		var self = this;
		setInterval(function() {
			axios.get("/status", { timeout: 450 }).then(function(response) {
				self.computer_control = response.data.computer_control;
				self.filament_status_message = response.data.filament_status_message;
				self.active_users = response.data.active_users;
			})
			.catch(function(error) {
				if (error.response === undefined) {
					self.filament_status_message = "Failed to communicate with server.";
				}
				else {
					self.filament_status_message = error.response.data;
				}
			});
		}, 500);
	}
};

Vue.createApp(app).mount("#vue_div");
