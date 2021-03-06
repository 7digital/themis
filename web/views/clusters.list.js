(function () {
  'use strict';

  var app = angular.module('app');

  app.controller('clusterAutoscaleCtrl', function($scope, appConfig) {
    $scope.$watch('cluster.autoscale', function(newVal, oldVal) {
      if(typeof(oldVal) != "undefined") {
        appConfig.getConfigValue('autoscaling_clusters').then(function(autoscaleClusters) {
          autoscaleClusters = autoscaleClusters.split(/\s*,\s*/);
          $scope.arrayRemove(autoscaleClusters, $scope.cluster.id);
          if(newVal) {
            autoscaleClusters.push($scope.cluster.id);
          }
          autoscaleClusters = autoscaleClusters.join(',');
          appConfig.setConfigValue('autoscaling_clusters', autoscaleClusters).then(function(config) {
            console.log("Successfully updated configuration.");
          });
        });
      }
    });
  });

  app.controller('clustersListCtrl', function($scope, restClient, appConfig) {

    var client = restClient;

    var loadClusterData = function(data) {
      $scope.$apply(function() {
        $scope.clusters = data.results;
        appConfig.getConfigValue('autoscaling_clusters').then(function(autoscaleClusters) {
          autoscaleClusters = autoscaleClusters.split(/\s*,\s*/);
          $scope.clusters.forEach(function(cluster) {
            cluster.autoscale = autoscaleClusters.indexOf(cluster.id) >= 0;
          });
          $scope.$apply();
        });
      });
    };

    $scope.refresh = function() {
      client.then(function(client) {
        $scope.loading = true;

        /* load current state */
        client.default.getClusters().then(function(obj) {
          $scope.loading = false;
          loadClusterData(obj.obj);
        }, function(err) {
          $scope.loading = false;
          console.log(err);
        });

      });
    };

    $scope.refresh();

  });

})();
