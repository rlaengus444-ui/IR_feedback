"""Synchronous Excel group for session tests; no Excel process is started."""


class InlineWorkbookGroup:
    def __init__(self, spec, target_temperature, maximum_ir_percent, stop_event, factory):
        self.spec, self.target, self.maximum = spec, target_temperature, maximum_ir_percent
        self.stop, self.factory = stop_event, factory
        self.controllers, self.event, self.state = {}, None, 'new'
        self.history = False

    def start(self, heaters):
        try:
            for heater in heaters:
                self.controllers[heater] = self.factory(self.spec, self.target,
                    maximum_ir_percent=self.maximum, stop_event=self.stop)
            self.state, self.event = 'ready', {'kind': 'ready', 'ticket': None}
        except Exception as error:
            self.state, self.event = 'error', {'kind': 'error', 'ticket': None, 'error': error}
        return True

    def poll(self):
        event, self.event = self.event, None
        return event

    def submit(self, ticket, layer, temperature, powers, target, slice_features=None):
        if self.state != 'ready':
            return False
        try:
            results = {}
            for heater, controller in self.controllers.items():
                controller.set_target_temperature(target)
                kwargs = {} if slice_features is None else {'slice_features': slice_features}
                results[heater] = controller.process(layer, temperature, powers[heater], **kwargs)
            self.history = True
            self.event = {'kind': 'result', 'ticket': ticket, 'results': results}
        except Exception as error:
            self.state, self.event = 'error', {'kind': 'error', 'ticket': ticket, 'error': error}
        return True

    def reset(self, ticket=None, target=None):
        if self.state != 'ready':
            return False
        try:
            if target is not None:
                self.target = target
            if self.history:
                for controller in self.controllers.values():
                    controller.reset()
            self.history = False
            self.event = {'kind': 'ready', 'ticket': ticket}
        except Exception as error:
            self.state, self.event = 'error', {'kind': 'error', 'ticket': ticket, 'error': error}
        return True

    def describe(self):
        return {'state': self.state}

    def close(self):
        if self.state != 'closed':
            self.state = 'closed'
            for controller in self.controllers.values():
                controller.close(force=True)
