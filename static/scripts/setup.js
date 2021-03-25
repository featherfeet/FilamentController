const app = {
	data: function() {
		return {
			max_virtual_knob_value: 0.0,
			current_max_dac_value: 0.0,
			error_message: "",
			warning_message: ""
		};
	},
	methods: {
		submitNewSettings: function() {
			var self = this;
			var formData = new FormData();
			formData.append("max_virtual_knob_value", parseFloat(self.max_virtual_knob_value));
			axios({
				method: "post",
				url: "/setup",
				data: formData,
				headers: { "Content-Type": "multipart/form-data" }
			}).then(function(response) {
				window.location.href = "/";
			})
			.catch(function(error) {
				if (error.response === undefined) {
					self.error_message = "Failed to communicate with server.";
				}
				else {
					self.error_message = error.response.data;
				}
			});
		}
	},
	mounted: function() {
		var self = this;
		axios.get("/status", { timeout: 1000 }).then(function(response) {
			self.max_virtual_knob_value = ((response.data.max_dac_value / (Math.pow(2, response.data.dac_bits) - 1)) * 10.0).toPrecision(2);
			self.current_max_dac_value = response.data.max_dac_value;
		})
		.catch(function(error) {
			if (error.response === undefined) {
				self.error_message = "Failed to communicate with server.";
			}
			else {
				self.error_message = error.response.data;
			}
		});
		setInterval(function() {
			axios.get("/status", { timeout: 450 } ).then(function(response) {
				if (self.current_max_dac_value !== response.data.max_dac_value) {
					self.warning_message = "Warning: Someone else on the system changed the max virtual knob value setting while you were on this page.";
				}
			})
			.catch(function(error) {
				if (error.response === undefined) {
					self.error_message = "Failed to communicate with server.";
				}
				else {
					self.error_message = error.response.data;
				}
			});
		}, 500);
	}
};

Vue.createApp(app).mount("#vue_div");
